"""A steady Gaussian plume for a fire that has just started.

No training data. Each detection is a continuous point source. The target is
affected only after the travel time (distance / wind speed) and only when it
sits inside a downwind sector.

NWS reports the direction the wind comes FROM. The plume travels the opposite
way: a west wind (270°) blows toward the east (90°).

Inside that sector the outlook uses the ground-level centerline concentration.
The sector is the wind-direction uncertainty (a compass point is already 22.5°
wide, and the gate here is ±30°). A class-D Pasquill-Gifford plume is only
about ±10° wide, so stacking a crosswind Gaussian on top of the sector would
report ~0 for almost every fire. ``ground_level_concentration_ug_m3`` still
takes a crosswind distance for the underlying equation.

Emission assumption
-------------------
``Q (kg/s) = emission_factor_kg_per_mj * FRP (MW)``.

One megawatt of fire radiative power is one megajoule per second, so the factor
is kilograms of PM2.5 per megajoule radiated. The default 0.02 kg/MJ is a
screening knob, not a fit:

* radiated energy corresponds to on the order of 0.37 kg of dry fuel per MJ
  (the coefficient often traced to Wooster et al., 2005);
* a blended wildfire PM2.5 emission factor of about 50 g per kg of fuel sits
  between flaming (~10–20 g/kg) and a smoldering-heavy fire (higher);
* 0.37 kg/MJ * 0.05 kg/kg ≈ 0.019 kg/MJ, rounded to 0.02.

Real smoke per megawatt varies several-fold with fuel and combustion phase.
Change it with ``--emission-factor`` or ``ModelConfig``.

Dispersion
----------
Rural (open-country) Briggs (1973) fits to the Pasquill-Gifford sigmas.
Default stability is class D, neutral. The formulas are the usual
``sigma = a * x * (1 + b * x) ** c`` with ``x`` in meters. They were built for
roughly 100 m to 10 km. This tool still evaluates them farther out and treats
that as a scale, not a calibrated regional model.

Ground-level concentration with an image source (receptor height z = 0)::

    C = Q / (pi * u * sy * sz) * exp(-y^2 / (2 sy^2)) * exp(-H^2 / (2 sz^2))

``Q`` is µg/s, ``u`` is m/s, ``sy`` and ``sz`` are meters, ``C`` is µg/m³.
``H`` is a configurable effective plume height, default 500 m. It stands in
for plume rise. It is not computed from FRP.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from wildfirebot.geo import angular_separation_deg

# (a, b, c) for sigma_y and sigma_z. Rural Briggs (1973).
# Class A/B vertical curves are linear: b = 0, c = 1, so sigma_z = a * x.
_BRIGGS_RURAL: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "A": ((0.22, 0.0001, -0.5), (0.20, 0.0, 1.0)),
    "B": ((0.16, 0.0001, -0.5), (0.12, 0.0, 1.0)),
    "C": ((0.11, 0.0001, -0.5), (0.08, 0.0002, -0.5)),
    "D": ((0.08, 0.0001, -0.5), (0.06, 0.0015, -0.5)),
    "E": ((0.06, 0.0001, -0.5), (0.03, 0.0003, -1.0)),
    "F": ((0.04, 0.0001, -0.5), (0.016, 0.0003, -1.0)),
}

DEFAULT_EMISSION_FACTOR_KG_PER_MJ = 0.02
DEFAULT_PLUME_HEIGHT_M = 500.0
DEFAULT_STABILITY = "D"
DEFAULT_DOWNWIND_HALF_ANGLE_DEG = 30.0
DEFAULT_MIN_WIND_SPEED_M_S = 0.5
DEFAULT_MIN_DOWNWIND_M = 100.0

EMISSION_ASSUMPTION = (
    "Q (kg/s) = 0.02 kg/MJ × FRP (MW). One megawatt is one megajoule per second. "
    "0.02 kg/MJ is about 0.37 kg of fuel per MJ of radiated energy times about "
    "50 g of PM2.5 per kg of fuel, a blend of flaming and smoldering. It is a "
    "tunable assumption, not a fitted emission factor."
)


@dataclass(frozen=True)
class ModelConfig:
    emission_factor_kg_per_mj: float = DEFAULT_EMISSION_FACTOR_KG_PER_MJ
    plume_height_m: float = DEFAULT_PLUME_HEIGHT_M
    stability: str = DEFAULT_STABILITY
    downwind_half_angle_deg: float = DEFAULT_DOWNWIND_HALF_ANGLE_DEG
    min_wind_speed_m_s: float = DEFAULT_MIN_WIND_SPEED_M_S
    min_downwind_m: float = DEFAULT_MIN_DOWNWIND_M

    def validated(self) -> ModelConfig:
        stability = self.stability.upper()
        if stability not in _BRIGGS_RURAL:
            allowed = ", ".join(sorted(_BRIGGS_RURAL))
            raise ValueError(f"stability must be one of {allowed}")
        if self.emission_factor_kg_per_mj < 0:
            raise ValueError("emission factor cannot be negative")
        if self.plume_height_m < 0:
            raise ValueError("plume height cannot be negative")
        if self.downwind_half_angle_deg < 0 or self.downwind_half_angle_deg > 180:
            raise ValueError("downwind half-angle must be between 0 and 180 degrees")
        if self.min_wind_speed_m_s <= 0:
            raise ValueError("minimum wind speed must be positive")
        if self.min_downwind_m <= 0:
            raise ValueError("minimum downwind distance must be positive")
        return ModelConfig(
            emission_factor_kg_per_mj=self.emission_factor_kg_per_mj,
            plume_height_m=self.plume_height_m,
            stability=stability,
            downwind_half_angle_deg=self.downwind_half_angle_deg,
            min_wind_speed_m_s=self.min_wind_speed_m_s,
            min_downwind_m=self.min_downwind_m,
        )


@dataclass(frozen=True)
class SmokeSource:
    """One fire reduced to the numbers the plume needs."""

    distance_m: float
    bearing_deg: float
    frp_mw: float


def wind_toward_deg(wind_from_deg: float) -> float:
    """Direction the wind is blowing toward. NWS reports where it comes from."""

    return (wind_from_deg + 180.0) % 360.0


def is_downwind(
    bearing_fire_to_target_deg: float,
    wind_from_deg: float,
    half_angle_deg: float = DEFAULT_DOWNWIND_HALF_ANGLE_DEG,
) -> bool:
    """True when the bearing from the fire to the target is inside the sector.

    The sector is centered on the wind's direction of travel, not on the
    direction the wind comes from. The edge (exactly ``half_angle_deg``) counts
    as downwind.
    """

    toward = wind_toward_deg(wind_from_deg)
    return angular_separation_deg(bearing_fire_to_target_deg, toward) <= half_angle_deg


def arrival_hours(distance_m: float, wind_speed_m_s: float) -> float:
    """Travel time in hours. Wind speed must be positive; callers floor calm wind."""

    if wind_speed_m_s <= 0:
        raise ValueError("wind speed must be positive to compute an arrival time")
    if distance_m < 0:
        raise ValueError("distance cannot be negative")
    return (distance_m / wind_speed_m_s) / 3600.0


def _sigma(x_m: float, a: float, b: float, c: float) -> float:
    return a * x_m * (1.0 + b * x_m) ** c


def dispersion_sigmas_m(downwind_m: float, stability: str = DEFAULT_STABILITY) -> tuple[float, float]:
    """Return ``(sigma_y, sigma_z)`` in meters for a downwind distance in meters."""

    if downwind_m <= 0:
        raise ValueError("downwind distance must be positive")
    key = stability.upper()
    try:
        (ay, by, cy), (az, bz, cz) = _BRIGGS_RURAL[key]
    except KeyError as exc:
        allowed = ", ".join(sorted(_BRIGGS_RURAL))
        raise ValueError(f"stability must be one of {allowed}") from exc
    return _sigma(downwind_m, ay, by, cy), _sigma(downwind_m, az, bz, cz)


def ground_level_concentration_ug_m3(
    emission_kg_s: float,
    wind_speed_m_s: float,
    downwind_m: float,
    crosswind_m: float = 0.0,
    plume_height_m: float = 0.0,
    stability: str = DEFAULT_STABILITY,
) -> float:
    """Ground-level Gaussian concentration in µg/m³, including ground reflection.

    ``emission_kg_s`` is the PM2.5 mass emission rate. ``downwind_m`` is the
    along-wind distance and ``crosswind_m`` is the off-axis distance. Both
    distances are meters. A negative emission or wind speed is rejected.
    """

    if emission_kg_s < 0:
        raise ValueError("emission rate cannot be negative")
    if wind_speed_m_s <= 0:
        raise ValueError("wind speed must be positive")
    if plume_height_m < 0:
        raise ValueError("plume height cannot be negative")
    if emission_kg_s == 0:
        return 0.0
    sigma_y, sigma_z = dispersion_sigmas_m(downwind_m, stability)
    emission_ug_s = emission_kg_s * 1.0e9
    lateral = math.exp(-(crosswind_m**2) / (2.0 * sigma_y**2))
    vertical = math.exp(-(plume_height_m**2) / (2.0 * sigma_z**2))
    return (emission_ug_s / (math.pi * wind_speed_m_s * sigma_y * sigma_z)) * lateral * vertical


def emission_kg_s(frp_mw: float, emission_factor_kg_per_mj: float) -> float:
    """PM2.5 emission rate. FRP in MW times kg/MJ gives kg/s."""

    if frp_mw < 0:
        raise ValueError("FRP cannot be negative")
    return emission_factor_kg_per_mj * frp_mw


def source_increment_ug_m3(
    source: SmokeSource,
    wind_from_deg: float,
    wind_speed_m_s: float,
    horizon_hours: float,
    config: ModelConfig | None = None,
) -> float:
    """PM2.5 increment from one fire at one horizon.

    Zero when the target is outside the downwind sector, or when the smoke
    has not had time to arrive. A fire within about a meter of the target is
    treated as already here, because the bearing is meaningless at that range.
    Dispersion distances shorter than ``min_downwind_m`` are lifted to that
    floor so a point source on top of the receptor does not explode.
    """

    model = (config or ModelConfig()).validated()
    if wind_speed_m_s <= 0:
        raise ValueError("wind speed must be positive")
    if horizon_hours < 0:
        raise ValueError("horizon cannot be negative")
    on_top = source.distance_m <= 1.0
    if not on_top and not is_downwind(
        source.bearing_deg, wind_from_deg, model.downwind_half_angle_deg
    ):
        return 0.0
    travel_hours = arrival_hours(source.distance_m, wind_speed_m_s)
    if travel_hours > horizon_hours:
        return 0.0
    rate = emission_kg_s(source.frp_mw, model.emission_factor_kg_per_mj)
    return ground_level_concentration_ug_m3(
        emission_kg_s=rate,
        wind_speed_m_s=wind_speed_m_s,
        downwind_m=max(source.distance_m, model.min_downwind_m),
        crosswind_m=0.0,
        plume_height_m=model.plume_height_m,
        stability=model.stability,
    )


def plume_increment_ug_m3(
    sources: list[SmokeSource],
    wind_from_deg: float,
    wind_speed_m_s: float,
    horizon_hours: float,
    config: ModelConfig | None = None,
) -> float:
    """Sum of every fire's ground-level increment at one horizon."""

    total = 0.0
    for source in sources:
        total += source_increment_ug_m3(
            source, wind_from_deg, wind_speed_m_s, horizon_hours, config
        )
    return total
