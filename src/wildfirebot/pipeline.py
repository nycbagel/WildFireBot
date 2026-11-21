"""Assemble detections, winds, and monitor readings into one forecast."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from wildfirebot import DISCLAIMER
from wildfirebot.fires.cluster import Cluster, cluster_hotspots
from wildfirebot.geo import bbox_from_point, haversine_km
from wildfirebot.model.aqi import category_for_aqi, pm25_to_aqi
from wildfirebot.sources.airnow import AirNowClient, empty_observations
from wildfirebot.sources.base import DataSource, TimeRange, floor_hour, optional_float
from wildfirebot.sources.firms import FirmsClient
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.transport.score import (
    ScoreConfig,
    exposure_label,
    score_clusters,
    trajectory_points,
)
from wildfirebot.transport.winds import WindHour, WindSummary, prepare_winds, summarize_winds

BACKFILL_NOTE = "wind estimated: earlier hours copied from the backup feed's first hour"


@dataclass(frozen=True)
class HourScore:
    time: datetime
    score: float | None
    label: str
    speed_m_s: float | None
    direction_deg: float | None
    blh_m: float | None
    notes: tuple[str, ...]
    top_cluster: int | None
    top_contribution: float | None


@dataclass
class ForecastReport:
    latitude: float
    longitude: float
    hours: int
    radius_km: float
    generated_at: datetime
    data_mode: str
    clusters: list[Cluster]
    hourly: list[HourScore]
    wind_summary: WindSummary | None
    observations: pd.DataFrame
    notices: list[str]
    degraded: bool
    disclaimer: str = DISCLAIMER


def build_report(
    *,
    latitude: float,
    longitude: float,
    hours: int,
    radius_km: float,
    detection_days: int,
    cluster_km: float,
    lookback_hours: int,
    now: datetime,
    firms: FirmsClient,
    weather: DataSource,
    airnow: AirNowClient | None,
    openaq: OpenAQClient | None,
    data_mode: str,
    score_config: ScoreConfig | None = None,
    max_wind_gap_hours: int = 3,
) -> ForecastReport:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    bbox = bbox_from_point(latitude, longitude, radius_km)
    fire_range = TimeRange(now - timedelta(days=detection_days), now)
    origin = floor_hour(now)
    wind_range = TimeRange(
        origin - timedelta(hours=max(lookback_hours - 1, 0)),
        origin + timedelta(hours=max(hours - 1, 0)),
    )
    obs_range = TimeRange(now - timedelta(days=1), now + timedelta(hours=1))

    notices: list[str] = []
    degraded = False

    fires = firms.fetch(bbox, fire_range)
    notices.extend(fires.notices)
    degraded = degraded or fires.degraded
    fires_available = bool(fires.sources_used)

    winds = weather.fetch(bbox, wind_range)
    notices.extend(winds.notices)
    degraded = degraded or winds.degraded
    prepared = (
        prepare_winds(winds.data, max_gap_hours=max_wind_gap_hours) if not winds.data.empty else []
    )
    wind_by_time = {hour.time: hour for hour in prepared}

    observations, obs_degraded, obs_notices = _observations(
        airnow, openaq, bbox, obs_range, latitude, longitude
    )
    notices.extend(obs_notices)
    degraded = degraded or obs_degraded

    clusters = cluster_hotspots(fires.data, cluster_km) if fires_available else []
    forecast_times = [origin + timedelta(hours=offset) for offset in range(hours)]
    window_winds = [wind_by_time[moment] for moment in forecast_times if moment in wind_by_time]
    summary = summarize_winds(window_winds) if window_winds else None

    hourly: list[HourScore] = []
    interpolated = 0
    substituted = 0
    missing_wind = 0
    for moment in forecast_times:
        hour_score, flags = _score_hour(
            moment=moment,
            latitude=latitude,
            longitude=longitude,
            lookback_hours=lookback_hours,
            wind_by_time=wind_by_time,
            clusters=clusters,
            fires_available=fires_available,
            config=score_config or ScoreConfig(),
        )
        hourly.append(hour_score)
        interpolated += int(flags["interpolated"])
        substituted += int(flags["substituted"])
        missing_wind += int(flags["missing"])

    if interpolated:
        notices.append(
            f"{interpolated} forecast hour(s) use interpolated wind because the feed left a short gap. "
            "Those hours are flagged in the table."
        )
    if substituted:
        degraded = True
        notices.append(
            f"{substituted} forecast hour(s) had no 10 m wind after interpolation, so the 850 hPa wind "
            "was used for transport instead."
        )
    if missing_wind:
        degraded = True
        notices.append(
            f"{missing_wind} forecast hour(s) have no usable wind, so they have no smoke score."
        )
    if fires_available and not clusters:
        notices.append(
            "No fire detections passed quality checks inside the search radius. "
            "Scores stay at zero, which does not prove the air is clear."
        )
    if any(cluster.frp_mw <= 0 for cluster in clusters):
        notices.append(
            "Some clusters have no FRP, so they are listed but do not add to the smoke score. "
            "Landsat detections do not include FRP."
        )

    return ForecastReport(
        latitude=latitude,
        longitude=longitude,
        hours=hours,
        radius_km=radius_km,
        generated_at=now,
        data_mode=data_mode,
        clusters=clusters,
        hourly=hourly,
        wind_summary=summary,
        observations=observations,
        notices=_unique(notices),
        degraded=degraded,
    )


def _score_hour(
    *,
    moment: datetime,
    latitude: float,
    longitude: float,
    lookback_hours: int,
    wind_by_time: dict[datetime, WindHour],
    clusters: list[Cluster],
    fires_available: bool,
    config: ScoreConfig,
) -> tuple[HourScore, dict[str, bool]]:
    notes: list[str] = []
    flags = {"interpolated": False, "substituted": False, "missing": False}
    if not fires_available:
        sample = wind_by_time.get(moment)
        notes.append("fire detections unavailable")
        if sample is None or sample.transport_source == "missing":
            flags["missing"] = True
            notes.append("wind missing")
            return _empty_hour(moment, notes), flags
        if sample.interpolated_10m or sample.interpolated_blh:
            notes.append("wind interpolated")
            flags["interpolated"] = True
        if sample.backfilled:
            notes.append(BACKFILL_NOTE)
        if sample.transport_source == "850hPa":
            notes.append("used 850 hPa wind")
            flags["substituted"] = True
        return (
            HourScore(
                time=moment,
                score=None,
                label="unavailable",
                speed_m_s=sample.transport_speed,
                direction_deg=sample.transport_direction,
                blh_m=sample.blh_m,
                notes=tuple(_unique(notes)),
                top_cluster=None,
                top_contribution=None,
            ),
            flags,
        )

    steps: list[tuple[float, float]] = []
    arrival: WindHour | None = None
    shortened = False
    backfilled = False
    for lag in range(lookback_hours):
        sample = wind_by_time.get(moment - timedelta(hours=lag))
        if sample is None or sample.transport_source == "missing" or sample.transport_speed is None:
            shortened = lag > 0
            if lag == 0:
                flags["missing"] = True
                notes.append("wind missing")
                return _empty_hour(moment, notes), flags
            break
        if lag == 0:
            arrival = sample
        if sample.interpolated_10m or (
            sample.transport_source == "850hPa" and sample.interpolated_850
        ):
            flags["interpolated"] = True
        if sample.transport_source == "850hPa":
            flags["substituted"] = True
        backfilled = backfilled or sample.backfilled
        direction = sample.transport_direction
        speed = sample.transport_speed
        assert direction is not None and speed is not None
        steps.append((speed, direction))

    if flags["interpolated"]:
        notes.append("wind interpolated")
    if flags["substituted"]:
        notes.append("used 850 hPa wind")
    if backfilled:
        notes.append(BACKFILL_NOTE)
    if shortened:
        notes.append("back-trajectory shortened by a wind gap")
    blh = None if arrival is None else arrival.blh_m
    if arrival is not None and arrival.interpolated_blh:
        notes.append("boundary layer interpolated")
        flags["interpolated"] = True

    points = trajectory_points(latitude, longitude, steps)
    total, parts, blh_note = score_clusters(
        (latitude, longitude), points, clusters, moment, blh, config
    )
    if blh_note:
        notes.append(blh_note)
    top_cluster = parts[0][0] if parts else None
    top_contribution = parts[0][1] if parts else None
    speed = None if arrival is None else arrival.transport_speed
    direction = None if arrival is None else arrival.transport_direction
    return (
        HourScore(
            time=moment,
            score=total,
            label=exposure_label(total),
            speed_m_s=speed,
            direction_deg=direction,
            blh_m=blh,
            notes=tuple(_unique(notes)),
            top_cluster=top_cluster,
            top_contribution=top_contribution,
        ),
        flags,
    )


def _empty_hour(moment: datetime, notes: list[str]) -> HourScore:
    return HourScore(
        time=moment,
        score=None,
        label="unavailable",
        speed_m_s=None,
        direction_deg=None,
        blh_m=None,
        notes=tuple(_unique(notes)),
        top_cluster=None,
        top_contribution=None,
    )


def _observations(airnow, openaq, bbox, time_range, latitude: float, longitude: float):
    frames = []
    notices: list[str] = []
    degraded = False
    for client in (airnow, openaq):
        if client is None:
            continue
        result = client.fetch(bbox, time_range)
        notices.extend(result.notices)
        degraded = degraded or result.degraded
        if not result.data.empty:
            frames.append(result.data)
    if not frames:
        return empty_observations(), degraded, notices
    combined = pd.concat(frames, ignore_index=True)
    return _annotate(combined, latitude, longitude), degraded, notices


def _annotate(frame: pd.DataFrame, latitude: float, longitude: float) -> pd.DataFrame:
    rows = []
    for row in frame.to_dict("records"):
        pm25 = optional_float(row.get("pm25"))
        reported = optional_float(row.get("reported_aqi"))
        aqi = None
        category = None
        aqi_source = ""
        if pm25 is not None and pm25 >= 0:
            converted = pm25_to_aqi(pm25)
            aqi = converted.aqi
            category = converted.category
            aqi_source = "breakpoint"
        elif reported is not None:
            aqi = int(round(reported))
            category = category_for_aqi(aqi)
            aqi_source = "reported"
        site_lat = optional_float(row.get("latitude"))
        site_lon = optional_float(row.get("longitude"))
        distance = None
        if site_lat is not None and site_lon is not None:
            distance = haversine_km(latitude, longitude, site_lat, site_lon)
        updated = dict(row)
        updated["aqi"] = aqi
        updated["category"] = category
        updated["aqi_source"] = aqi_source
        updated["distance_km"] = distance
        rows.append(updated)
    annotated = pd.DataFrame(rows)
    if "distance_km" in annotated.columns:
        annotated = annotated.sort_values("distance_km", na_position="last")
    return annotated.reset_index(drop=True)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
