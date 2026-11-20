"""Smoke AQI forecast: background PM2.5 plus a Gaussian plume from each fire cluster.

This sits next to the relative smoke-exposure score in `pipeline.py`. It reuses the
same FIRMS clustering, Open-Meteo winds, and AirNow/OpenAQ readings, then turns FRP
into µg/m³ with `model.plume` and converts the sum to the EPA AQI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pandas as pd

from wildfirebot.fires.cluster import Cluster, cluster_hotspots
from wildfirebot.geo import bbox_from_point
from wildfirebot.model.aqi import pm25_to_aqi
from wildfirebot.model.plume import DEFAULT_HORIZONS, FireContribution, PlumeConfig, fire_pm25_at
from wildfirebot.model.terrain import terrain_adjustment
from wildfirebot.pipeline import _observations
from wildfirebot.sources.airnow import AirNowClient
from wildfirebot.sources.base import TimeRange, floor_hour, optional_float
from wildfirebot.sources.firms import FirmsClient
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient
from wildfirebot.sources.openmeteo_aq import OpenMeteoAirQualityClient
from wildfirebot.transport.winds import WindHour, prepare_winds

DEFAULT_BACKGROUND_PM25 = 8.0
MAX_WIND_OFFSET_HOURS = 3
MAX_BASELINE_OFFSET_HOURS = 3

PLUME_DISCLAIMER = (
    "This AQI is a screening estimate: forecast background PM2.5 plus a Gaussian plume "
    "computed from satellite fire radiative power and forecast wind. It is not a health "
    "advisory and not a regulatory AQI. Check AirNow and local smoke outlooks."
)


@dataclass(frozen=True)
class HorizonForecast:
    horizon_hours: int
    time: datetime
    wind_speed_m_s: float | None
    wind_from_deg: float | None
    baseline_pm25: float
    baseline_source: str
    fire_pm25: float
    pm25: float
    aqi: int
    category: str
    contributions: tuple[FireContribution, ...]
    notes: tuple[str, ...] = ()


@dataclass
class AqiForecastReport:
    latitude: float
    longitude: float
    radius_km: float
    generated_at: datetime
    data_mode: str
    config: PlumeConfig
    clusters: list[Cluster]
    horizons: list[HorizonForecast]
    observations: pd.DataFrame
    notices: list[str] = field(default_factory=list)
    degraded: bool = False
    terrain_notes: list[str] = field(default_factory=list)
    disclaimer: str = PLUME_DISCLAIMER


def build_aqi_forecast(
    *,
    latitude: float,
    longitude: float,
    radius_km: float,
    detection_days: int,
    cluster_km: float,
    now: datetime,
    firms: FirmsClient,
    weather: OpenMeteoClient,
    air_quality: OpenMeteoAirQualityClient | None,
    airnow: AirNowClient | None,
    openaq: OpenAQClient | None,
    data_mode: str,
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
    config: PlumeConfig | None = None,
    use_terrain: bool = True,
    max_terrain_clusters: int = 5,
) -> AqiForecastReport:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not horizons:
        raise ValueError("horizons cannot be empty")
    if any(hour < 0 or hour > 120 for hour in horizons):
        raise ValueError("horizons must be between 0 and 120 hours")
    cfg = config or PlumeConfig()
    now = now.astimezone(UTC)
    origin = floor_hour(now)
    ordered = sorted(set(int(hour) for hour in horizons))
    last = ordered[-1]
    bbox = bbox_from_point(latitude, longitude, radius_km)
    notices: list[str] = []
    degraded = False

    fires = firms.fetch(bbox, TimeRange(now - timedelta(days=detection_days), now))
    notices.extend(fires.notices)
    degraded = degraded or fires.degraded
    clusters = cluster_hotspots(fires.data, cluster_km) if fires.sources_used else []

    forecast_range = TimeRange(origin, origin + timedelta(hours=max(last, 1)))
    winds = weather.fetch(bbox, forecast_range)
    notices.extend(winds.notices)
    degraded = degraded or winds.degraded
    prepared = prepare_winds(winds.data) if not winds.data.empty else []

    baseline = pd.DataFrame(columns=["time", "pm25"])
    if air_quality is not None:
        background = air_quality.fetch(bbox, forecast_range)
        notices.extend(background.notices)
        degraded = degraded or background.degraded
        baseline = background.data

    observations, obs_degraded, obs_notices = _observations(
        airnow,
        openaq,
        bbox,
        TimeRange(now - timedelta(days=1), now + timedelta(hours=1)),
        latitude,
        longitude,
    )
    notices.extend(obs_notices)
    degraded = degraded or obs_degraded
    observed = _nearest_observed_pm25(observations)

    terrain: list[dict | None] | None = None
    terrain_notes: list[str] = []
    if use_terrain and clusters:
        nearest = sorted(
            range(len(clusters)),
            key=lambda i: (
                (clusters[i].latitude - latitude) ** 2 + (clusters[i].longitude - longitude) ** 2
            ),
        )[:max_terrain_clusters]
        terrain = [None] * len(clusters)
        for index in nearest:
            cluster = clusters[index]
            adjust = terrain_adjustment(
                cluster.latitude, cluster.longitude, latitude, longitude, cfg.plume_height_m
            )
            terrain[index] = adjust
            terrain_notes.extend(
                f"cluster {index} ({cluster.latitude:.3f}, {cluster.longitude:.3f}): {note}"
                for note in adjust.get("notes", [])
            )
            terrain_notes.append(
                f"cluster {index}: blocking {adjust['blocking_factor']}, class "
                f"{adjust['stability_class']}, mixing height {adjust['mixing_height_m']:.0f} m"
            )

    rows: list[HorizonForecast] = []
    no_wind = 0
    for hour in ordered:
        moment = origin + timedelta(hours=hour)
        notes: list[str] = []
        wind = _nearest_wind(prepared, moment)
        base, base_source = _baseline(baseline, moment, observed)
        if wind is None:
            no_wind += 1
            notes.append("wind unavailable, fire smoke not added")
            fire, parts = 0.0, []
            speed = direction = None
        else:
            speed, direction = wind.transport_speed, wind.transport_direction
            assert speed is not None and direction is not None
            fire, parts = fire_pm25_at(
                clusters, latitude, longitude, direction, speed, float(hour), cfg, terrain
            )
            if wind.transport_source == "850hPa":
                notes.append("used 850 hPa wind")
        total = base + fire
        converted = pm25_to_aqi(total)
        rows.append(
            HorizonForecast(
                horizon_hours=hour,
                time=moment,
                wind_speed_m_s=speed,
                wind_from_deg=direction,
                baseline_pm25=base,
                baseline_source=base_source,
                fire_pm25=fire,
                pm25=total,
                aqi=converted.aqi,
                category=converted.category,
                contributions=tuple(parts),
                notes=tuple(notes),
            )
        )

    if no_wind:
        degraded = True
        notices.append(
            f"{no_wind} horizon(s) have no forecast wind within {MAX_WIND_OFFSET_HOURS} hours, "
            "so only background PM2.5 is shown there."
        )
    if any(row.baseline_source == "default" for row in rows):
        degraded = True
        notices.append(
            f"No background PM2.5 was available for some horizons, so {DEFAULT_BACKGROUND_PM25} "
            "µg/m³ was assumed there."
        )
    if fires.sources_used and not clusters:
        notices.append("No fire detections passed quality checks inside the search radius.")
    if any(cluster.frp_mw <= 0 for cluster in clusters):
        notices.append("Clusters without FRP (Landsat-only) add no plume smoke.")
    if any(row.fire_pm25 >= cfg.max_fire_pm25 for row in rows):
        notices.append(f"Fire smoke hit the {cfg.max_fire_pm25:.0f} µg/m³ cap at some horizons.")

    return AqiForecastReport(
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        generated_at=now,
        data_mode=data_mode,
        config=cfg,
        clusters=clusters,
        horizons=rows,
        observations=observations,
        notices=list(dict.fromkeys(note for note in notices if note)),
        degraded=degraded,
        terrain_notes=terrain_notes,
    )


def _nearest_wind(prepared: list[WindHour], moment: datetime) -> WindHour | None:
    usable = [
        hour
        for hour in prepared
        if hour.transport_speed is not None and hour.transport_direction is not None
    ]
    if not usable:
        return None
    best = min(usable, key=lambda hour: abs((hour.time - moment).total_seconds()))
    if abs((best.time - moment).total_seconds()) > MAX_WIND_OFFSET_HOURS * 3600:
        return None
    return best


def _baseline(frame: pd.DataFrame, moment: datetime, observed: float | None) -> tuple[float, str]:
    if frame is not None and not frame.empty:
        times = pd.to_datetime(frame["time"], utc=True)
        offsets = (times - pd.Timestamp(moment)).abs()
        index = offsets.idxmin()
        if offsets[index] <= pd.Timedelta(hours=MAX_BASELINE_OFFSET_HOURS):
            value = optional_float(frame.loc[index, "pm25"])
            if value is not None:
                return value, "open-meteo forecast"
    if observed is not None:
        return observed, "nearest monitor (persistence)"
    return DEFAULT_BACKGROUND_PM25, "default"


def _nearest_observed_pm25(observations: pd.DataFrame) -> float | None:
    if observations is None or observations.empty or "pm25" not in observations.columns:
        return None
    for value in observations["pm25"]:
        number = optional_float(value)
        if number is not None and number >= 0:
            return number
    return None
