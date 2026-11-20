"""Steady-state Gaussian plume: fire radiative power to ground-level PM2.5.

Ground-level concentration from a continuous elevated point source with ground
reflection:

    C(x, y) = Q / (pi * u * sigma_y * sigma_z) * exp(-y^2 / (2 sigma_y^2)) * exp(-H^2 / (2 sigma_z^2))

x is the along-wind distance, y the crosswind offset, u the wind speed, H the
effective plume height, and sigma_y, sigma_z the Pasquill-Gifford dispersion
widths (Briggs 1973 open-country fits, x in metres).

Emission is Q [g/s] = emission_factor * FRP [MW]. The 5.5 g/s per MW default is a
biomass-burning rule of thumb: about 0.368 kg of dry matter burned per MJ of FRP
times about 15 g of PM2.5 per kg burned (temperate forest). It is a tunable
assumption. Real values vary with fuel, burn phase, and plume rise.

This needs no training data, so it works for a fire that started an hour ago.
It is a screening estimate, not a dispersion model run.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from wildfirebot.fires.cluster import Cluster
from wildfirebot.geo import haversine_km, initial_bearing

DEFAULT_HORIZONS: tuple[int, ...] = (0, 6, 12, 24, 48)
STABILITY_CLASSES = ("A", "B", "C", "D", "E", "F")

# Briggs open-country sigma_y = a x (1 + 0.0001 x)^-0.5
_SIGMA_Y_A = {"A": 0.22, "B": 0.16, "C": 0.11, "D": 0.08, "E": 0.06, "F": 0.04}
_SIGMA_Z: dict[str, Callable[[float], float]] = {
    "A": lambda x: 0.20 * x,
    "B": lambda x: 0.12 * x,
    "C": lambda x: 0.08 * x * (1 + 0.0002 * x) ** -0.5,
    "D": lambda x: 0.06 * x * (1 + 0.0015 * x) ** -0.5,
    "E": lambda x: 0.03 * x * (1 + 0.0003 * x) ** -1.0,
    "F": lambda x: 0.016 * x * (1 + 0.0003 * x) ** -1.0,
}


@dataclass(frozen=True)
class PlumeConfig:
    emission_factor_g_s_per_mw: float = 5.5
    stability: str = "D"
    plume_height_m: float = 200.0
    downwind_half_angle_deg: float = 30.0
    min_distance_km: float = 1.0
    min_wind_m_s: float = 1.0
    max_fire_pm25: float = 500.0
    max_frp_mw: float = 3000.0

    def __post_init__(self) -> None:
        if self.stability not in STABILITY_CLASSES:
            raise ValueError(f"stability must be one of {', '.join(STABILITY_CLASSES)}")
        if self.emission_factor_g_s_per_mw < 0:
            raise ValueError("emission factor cannot be negative")
        if self.plume_height_m < 0:
            raise ValueError("plume height cannot be negative")


@dataclass(frozen=True)
class FireContribution:
    cluster_index: int
    distance_km: float
    bearing_from_fire_deg: float
    off_axis_deg: float
    downwind: bool
    arrival_hours: float
    pm25: float


def sigmas(x_m: float, stability: str = "D") -> tuple[float, float]:
    """Pasquill-Gifford (sigma_y, sigma_z) in metres at along-wind distance `x_m`."""
    sigma_y = _SIGMA_Y_A[stability] * x_m * (1 + 0.0001 * x_m) ** -0.5
    return sigma_y, _SIGMA_Z[stability](x_m)


def travel_direction(wind_from_deg: float) -> float:
    """Wind direction is where it blows from. Smoke travels the opposite way."""
    return (wind_from_deg + 180.0) % 360.0


def angle_difference(first: float, second: float) -> float:
    diff = abs(first - second) % 360.0
    return 360.0 - diff if diff > 180.0 else diff


def arrival_hours(distance_km: float, wind_m_s: float, config: PlumeConfig | None = None) -> float:
    floor = (config or PlumeConfig()).min_wind_m_s
    return distance_km * 1000.0 / max(wind_m_s, floor) / 3600.0


def plume_pm25(
    frp_mw: float,
    distance_km: float,
    off_axis_deg: float,
    wind_m_s: float,
    config: PlumeConfig | None = None,
) -> float:
    """Ground-level PM2.5 in µg/m³ at the receptor from one fire. Zero without FRP."""
    cfg = config or PlumeConfig()
    if frp_mw is None or frp_mw <= 0 or math.isnan(frp_mw):
        return 0.0
    emission = cfg.emission_factor_g_s_per_mw * min(frp_mw, cfg.max_frp_mw)
    x = max(distance_km, cfg.min_distance_km) * 1000.0
    speed = max(wind_m_s, cfg.min_wind_m_s)
    along = x * math.cos(math.radians(off_axis_deg))
    if along <= 0:
        return 0.0
    cross = x * math.sin(math.radians(off_axis_deg))
    sigma_y, sigma_z = sigmas(along, cfg.stability)
    grams = (
        emission
        / (math.pi * speed * sigma_y * sigma_z)
        * math.exp(-(cross**2) / (2 * sigma_y**2))
        * math.exp(-(cfg.plume_height_m**2) / (2 * sigma_z**2))
    )
    return grams * 1e6


def fire_pm25_at(
    clusters: list[Cluster],
    latitude: float,
    longitude: float,
    wind_from_deg: float,
    wind_m_s: float,
    horizon_hours: float,
    config: PlumeConfig | None = None,
) -> tuple[float, list[FireContribution]]:
    """Sum smoke PM2.5 from every cluster for one wind state, capped.

    A cluster contributes only when the target sits within twice the downwind
    half-angle of its plume axis (the Gaussian tail is negligible beyond that) and
    the smoke has had time to arrive: distance / wind speed <= `horizon_hours`.
    """
    cfg = config or PlumeConfig()
    heading = travel_direction(wind_from_deg)
    total = 0.0
    parts: list[FireContribution] = []
    for index, cluster in enumerate(clusters):
        distance = haversine_km(cluster.latitude, cluster.longitude, latitude, longitude)
        bearing = initial_bearing(cluster.latitude, cluster.longitude, latitude, longitude)
        off_axis = angle_difference(bearing, heading)
        arrival = arrival_hours(distance, wind_m_s, cfg)
        pm25 = 0.0
        if off_axis <= 2 * cfg.downwind_half_angle_deg and arrival <= horizon_hours:
            pm25 = plume_pm25(cluster.frp_mw, distance, off_axis, wind_m_s, cfg)
        total += pm25
        parts.append(
            FireContribution(
                cluster_index=index,
                distance_km=distance,
                bearing_from_fire_deg=bearing,
                off_axis_deg=off_axis,
                downwind=off_axis <= cfg.downwind_half_angle_deg,
                arrival_hours=arrival,
                pm25=pm25,
            )
        )
    return min(total, cfg.max_fire_pm25), parts
