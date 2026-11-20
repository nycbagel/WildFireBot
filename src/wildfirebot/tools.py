"""Tool handlers shared by the CLI and the MCP server.

Forecast math stays in the pipeline so the human CLI and `wildfirebot-mcp`
cannot drift apart.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from wildfirebot.aqi_forecast import AqiForecastReport, build_aqi_forecast
from wildfirebot.http import FileCache, FixtureTransport, HttpClient, UrllibTransport
from wildfirebot.model.aqi import pm25_to_aqi
from wildfirebot.model.plume import DEFAULT_HORIZONS, STABILITY_CLASSES, PlumeConfig
from wildfirebot.pipeline import ForecastReport, build_report
from wildfirebot.sources.airnow import AirNowClient
from wildfirebot.sources.firms import DEFAULT_SOURCES, KNOWN_SOURCES, FirmsClient, parse_sources
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient
from wildfirebot.sources.openmeteo_aq import OpenMeteoAirQualityClient

DATA_MODE_LIVE = "live"
DATA_MODE_FIXTURES = "fixtures (not live)"

TOOLS: list[dict] = [
    {
        "name": "forecast_smoke",
        "description": (
            "Estimate relative wildfire smoke exposure at a latitude and longitude from "
            "NASA FIRMS detections and an Open-Meteo wind forecast. Returns fire clusters, "
            "a wind summary, an hourly smoke-exposure score, nearby PM2.5 readings, and "
            "degraded-mode notices. The score is not an AQI and not a health advisory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Target latitude"},
                "lon": {"type": "number", "description": "Target longitude"},
                "hours": {"type": "integer", "default": 48, "minimum": 1, "maximum": 120},
                "radius_km": {"type": "number", "default": 250},
                "days": {
                    "type": "integer",
                    "default": 2,
                    "description": "Days of FIRMS detections",
                },
                "lookback_hours": {"type": "integer", "default": 12},
            },
            "required": ["lat", "lon"],
            "additionalProperties": False,
        },
    },
    {
        "name": "forecast_smoke_aqi",
        "description": (
            "Forecast PM2.5 and US EPA AQI at a latitude and longitude for the next 0 to 48 "
            "hours. Adds a Gaussian plume from each NASA FIRMS fire cluster (emission from "
            "fire radiative power, carried by the Open-Meteo wind forecast) to the Open-Meteo "
            "background PM2.5 forecast. Works for brand-new fires because it needs no history. "
            "A screening estimate, not a health advisory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Target latitude"},
                "lon": {"type": "number", "description": "Target longitude"},
                "radius_km": {"type": "number", "default": 150},
                "horizons": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 120},
                    "default": list(DEFAULT_HORIZONS),
                    "description": "Forecast horizons in hours from now",
                },
                "days": {
                    "type": "integer",
                    "default": 1,
                    "description": "Days of FIRMS detections",
                },
                "stability": {
                    "type": "string",
                    "enum": list(STABILITY_CLASSES),
                    "default": "D",
                    "description": "Pasquill-Gifford stability class",
                },
                "plume_height_m": {"type": "number", "default": 200},
                "terrain": {
                    "type": "boolean",
                    "default": True,
                    "description": "Adjust each fire for ridges, stability, and mixing height",
                },
                "emission_factor": {
                    "type": "number",
                    "default": 5.5,
                    "description": "PM2.5 emission in g/s per MW of FRP",
                },
            },
            "required": ["lat", "lon"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pm25_to_aqi",
        "description": (
            "Convert a PM2.5 concentration in µg/m³ to the US EPA 2024 AQI using the "
            "breakpoint equation. This does not compute a 24-hour average."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "concentration": {
                    "type": "number",
                    "description": "PM2.5 concentration in micrograms per cubic meter",
                    "minimum": 0,
                },
            },
            "required": ["concentration"],
            "additionalProperties": False,
        },
    },
]


@dataclass
class Runtime:
    """Live or fixture-backed clients shared by the CLI and the future MCP server."""

    http: HttpClient
    firms: FirmsClient
    weather: OpenMeteoClient
    airnow: AirNowClient
    openaq: OpenAQClient
    data_mode: str
    air_quality: OpenMeteoAirQualityClient | None = None


def build_runtime(
    *,
    sources: list[str] | None = None,
    cluster_km: float = 3.0,
    min_confidence: float = 30.0,
    refresh: bool = False,
    cache_dir: Path | None = None,
    fixture_dir: Path | None = None,
    now: datetime | None = None,
) -> Runtime:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    backoff = float(os.environ.get("WILDFIREBOT_BACKOFF_SECONDS", "0.5"))
    if fixture_dir is not None:
        http = HttpClient(
            FixtureTransport(Path(fixture_dir), KNOWN_SOURCES),
            cache=None,
            backoff_seconds=backoff,
        )
        data_mode = DATA_MODE_FIXTURES
    else:
        cache_path = cache_dir or (Path.home() / ".cache" / "wildfirebot")
        try:
            cache = FileCache(cache_path)
        except OSError:
            cache = None
        http = HttpClient(UrllibTransport(), cache=cache, backoff_seconds=backoff)
        data_mode = DATA_MODE_LIVE
    chosen = sources or list(DEFAULT_SOURCES)
    return Runtime(
        http=http,
        firms=FirmsClient(
            api_key=os.environ.get("FIRMS_MAP_KEY", ""),
            http=http,
            sources=parse_sources(",".join(chosen)),
            separation_km=cluster_km,
            min_modis_confidence=min_confidence,
            refresh=refresh,
        ),
        weather=OpenMeteoClient(http, refresh=refresh, now=moment),
        airnow=AirNowClient(os.environ.get("AIRNOW_API_KEY", ""), http, refresh=refresh),
        openaq=OpenAQClient(os.environ.get("OPENAQ_API_KEY", ""), http, refresh=refresh),
        data_mode=data_mode,
        air_quality=OpenMeteoAirQualityClient(http, refresh=refresh, now=moment),
    )


def runtime_from_env() -> Runtime:
    """Build clients from the process environment.

    `WILDFIREBOT_FIXTURE_DIR` selects recorded responses for tests. Unset, the
    clients use live feeds. `WILDFIREBOT_NOW` overrides the clock when a test
    needs a fixed timestamp. Missing API keys are left empty; callers report
    that instead of raising.
    """
    fixture = os.environ.get("WILDFIREBOT_FIXTURE_DIR", "").strip()
    now_text = os.environ.get("WILDFIREBOT_NOW", "").strip()
    now = None
    if now_text:
        parsed = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
        now = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return build_runtime(
        fixture_dir=Path(fixture) if fixture else None,
        now=now,
    )


def call_tool(name: str, arguments: dict | None = None, runtime: Runtime | None = None) -> dict:
    """Run one tool and return a JSON-ready dict. This is the MCP `tools/call` body."""
    payload = arguments or {}
    if name == "pm25_to_aqi":
        if "concentration" not in payload:
            raise ValueError("pm25_to_aqi requires concentration")
        result = pm25_to_aqi(payload["concentration"])
        return {
            "aqi": result.aqi,
            "category": result.category,
            "concentration": result.concentration,
        }
    if name == "forecast_smoke":
        return forecast_smoke(payload, runtime)
    if name == "forecast_smoke_aqi":
        return forecast_smoke_aqi(payload, runtime)
    known = ", ".join(tool["name"] for tool in TOOLS)
    raise ValueError(f"Unknown tool {name!r}. Known tools: {known}")


def forecast_smoke(arguments: dict, runtime: Runtime | None = None) -> dict:
    latitude = float(arguments["lat"])
    longitude = float(arguments["lon"])
    hours = int(arguments.get("hours", 48))
    radius_km = float(arguments.get("radius_km", 250))
    days = int(arguments.get("days", 2))
    lookback = int(arguments.get("lookback_hours", 12))
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("latitude must be between -90 and 90 and longitude between -180 and 180")
    active = runtime or build_runtime(cluster_km=float(arguments.get("cluster_km", 3)))
    now = arguments.get("now")
    if now is None:
        now = os.environ.get("WILDFIREBOT_NOW", "").strip() or None
    if isinstance(now, str):
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        moment = parsed.astimezone(UTC)
    elif isinstance(now, datetime):
        moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    else:
        moment = datetime.now(UTC)
    report = build_report(
        latitude=latitude,
        longitude=longitude,
        hours=hours,
        radius_km=radius_km,
        detection_days=days,
        cluster_km=float(arguments.get("cluster_km", 3)),
        lookback_hours=lookback,
        now=moment,
        firms=active.firms,
        weather=active.weather,
        airnow=active.airnow,
        openaq=active.openaq,
        data_mode=active.data_mode,
    )
    return report_to_dict(report)


def report_to_dict(report: ForecastReport) -> dict:
    """JSON-ready forecast. Datetimes are UTC ISO-8601. Missing numbers are null."""
    peak = _peak(report)
    return {
        "latitude": report.latitude,
        "longitude": report.longitude,
        "hours": report.hours,
        "radius_km": report.radius_km,
        "generated_at": _iso(report.generated_at),
        "data_mode": report.data_mode,
        "degraded": report.degraded,
        "disclaimer": report.disclaimer,
        "peak": peak,
        "clusters": [_cluster_dict(report, cluster) for cluster in report.clusters],
        "wind": _wind_dict(report),
        "hourly": [_hour_dict(hour) for hour in report.hourly],
        "observations": _observations(report.observations),
        "notices": list(report.notices),
    }


def forecast_smoke_aqi(arguments: dict, runtime: Runtime | None = None) -> dict:
    latitude = float(arguments["lat"])
    longitude = float(arguments["lon"])
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("latitude must be between -90 and 90 and longitude between -180 and 180")
    radius_km = float(arguments.get("radius_km", 150))
    if not 1 <= radius_km <= 2000:
        raise ValueError("radius_km must be between 1 and 2000")
    horizons = [int(hour) for hour in arguments.get("horizons") or DEFAULT_HORIZONS]
    config = PlumeConfig(
        emission_factor_g_s_per_mw=float(arguments.get("emission_factor", 5.5)),
        stability=str(arguments.get("stability", "D")).upper(),
        plume_height_m=float(arguments.get("plume_height_m", 200)),
    )
    cluster_km = float(arguments.get("cluster_km", 3))
    active = runtime or build_runtime(cluster_km=cluster_km)
    air_quality = active.air_quality or OpenMeteoAirQualityClient(active.http)
    report = build_aqi_forecast(
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        detection_days=int(arguments.get("days", 1)),
        cluster_km=cluster_km,
        now=_moment(arguments.get("now")),
        firms=active.firms,
        weather=active.weather,
        air_quality=air_quality,
        airnow=active.airnow,
        openaq=active.openaq,
        data_mode=active.data_mode,
        horizons=horizons,
        config=config,
        use_terrain=bool(arguments.get("terrain", True)) and active.data_mode == DATA_MODE_LIVE,
    )
    return aqi_report_to_dict(report)


def aqi_report_to_dict(report: AqiForecastReport) -> dict:
    from wildfirebot.geo import compass_point, haversine_km

    first_wind = next(
        (row.wind_from_deg for row in report.horizons if row.wind_from_deg is not None), None
    )
    clusters = []
    current = report.horizons[0] if report.horizons else None
    for index, cluster in enumerate(report.clusters):
        part = None
        if current is not None:
            part = next((p for p in current.contributions if p.cluster_index == index), None)
        if part is None:
            distance = haversine_km(
                cluster.latitude, cluster.longitude, report.latitude, report.longitude
            )
        else:
            distance = part.distance_km
        bearing_to_fire = None
        if part is not None:
            bearing_to_fire = (part.bearing_from_fire_deg + 180.0) % 360.0
        clusters.append(
            {
                "latitude": cluster.latitude,
                "longitude": cluster.longitude,
                "distance_km": round(distance, 1),
                "direction_from_target": None
                if bearing_to_fire is None
                else compass_point(bearing_to_fire),
                "frp_mw": cluster.frp_mw,
                "hotspots": cluster.count,
                "downwind_now": None if part is None else part.downwind,
                "arrival_hours": None
                if part is None or not part.downwind
                else round(part.arrival_hours, 1),
                "last_seen": _iso(cluster.last_seen),
            }
        )
    clusters.sort(key=lambda item: item["distance_km"])
    horizons = [
        {
            "horizon_hours": row.horizon_hours,
            "time": _iso(row.time),
            "wind_speed_m_s": None if row.wind_speed_m_s is None else round(row.wind_speed_m_s, 1),
            "wind_from_deg": None if row.wind_from_deg is None else round(row.wind_from_deg),
            "baseline_pm25": round(row.baseline_pm25, 1),
            "baseline_source": row.baseline_source,
            "fire_pm25": round(row.fire_pm25, 1),
            "pm25": round(row.pm25, 1),
            "aqi": row.aqi,
            "category": row.category,
            "contributing_clusters": sum(1 for part in row.contributions if part.pm25 > 0),
            "notes": list(row.notes),
        }
        for row in report.horizons
    ]
    worst = max(horizons, key=lambda row: row["aqi"]) if horizons else None
    return {
        "latitude": report.latitude,
        "longitude": report.longitude,
        "radius_km": report.radius_km,
        "generated_at": _iso(report.generated_at),
        "data_mode": report.data_mode,
        "degraded": report.degraded,
        "disclaimer": report.disclaimer,
        "model": {
            "name": "gaussian-plume",
            "emission_factor_g_s_per_mw": report.config.emission_factor_g_s_per_mw,
            "stability": report.config.stability,
            "plume_height_m": report.config.plume_height_m,
            "downwind_half_angle_deg": report.config.downwind_half_angle_deg,
            "max_fire_pm25": report.config.max_fire_pm25,
            "max_frp_mw": report.config.max_frp_mw,
        },
        "worst": None
        if worst is None
        else {key: worst[key] for key in ("horizon_hours", "time", "aqi", "category", "pm25")},
        "prevailing_wind_from_deg": None if first_wind is None else round(first_wind),
        "fire_clusters": len(report.clusters),
        "clusters": clusters[:10],
        "horizons": horizons,
        "observations": _observations(report.observations)[:5],
        "terrain_notes": list(report.terrain_notes),
        "notices": list(report.notices),
    }


def _moment(now: object) -> datetime:
    if isinstance(now, str):
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _peak(report: ForecastReport) -> dict | None:
    scored = [hour for hour in report.hourly if hour.score is not None]
    if not scored:
        return None
    peak = max(scored, key=lambda hour: hour.score if hour.score is not None else -1.0)
    return {
        "time": _iso(peak.time),
        "score": peak.score,
        "label": peak.label,
    }


def _cluster_dict(report: ForecastReport, cluster) -> dict:
    from wildfirebot.geo import compass_point, haversine_km, initial_bearing

    distance = haversine_km(report.latitude, report.longitude, cluster.latitude, cluster.longitude)
    bearing = initial_bearing(
        report.latitude, report.longitude, cluster.latitude, cluster.longitude
    )
    return {
        "latitude": cluster.latitude,
        "longitude": cluster.longitude,
        "distance_km": round(distance, 1),
        "bearing_deg": round(bearing, 1),
        "direction": compass_point(bearing),
        "frp_mw": cluster.frp_mw,
        "hotspots": cluster.count,
        "sources": list(cluster.sources),
        "processing_levels": list(cluster.processing_levels),
        "first_seen": _iso(cluster.first_seen),
        "last_seen": _iso(cluster.last_seen),
    }


def _wind_dict(report: ForecastReport) -> dict | None:
    summary = report.wind_summary
    if summary is None:
        return None
    return {
        "speed_10m_mean": summary.speed_10m_mean,
        "direction_10m_prevailing": summary.direction_10m_prevailing,
        "speed_850hPa_mean": summary.speed_850_mean,
        "direction_850hPa_prevailing": summary.direction_850_prevailing,
        "boundary_layer_mean_m": summary.blh_mean,
        "boundary_layer_min_m": summary.blh_min,
        "boundary_layer_max_m": summary.blh_max,
        "interpolated_hours": summary.interpolated_hours,
        "substituted_hours": summary.substituted_hours,
        "missing_transport_hours": summary.missing_transport_hours,
    }


def _hour_dict(hour) -> dict:
    return {
        "time": _iso(hour.time),
        "score": hour.score,
        "label": hour.label,
        "wind_speed_m_s": hour.speed_m_s,
        "wind_direction_deg": hour.direction_deg,
        "boundary_layer_m": hour.blh_m,
        "notes": list(hour.notes),
    }


def _observations(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    rows = []
    for row in frame.to_dict("records"):
        rows.append(
            {
                "site_name": row.get("site_name") or "",
                "source": row.get("source") or "",
                "latitude": _number(row.get("latitude")),
                "longitude": _number(row.get("longitude")),
                "distance_km": _number(row.get("distance_km")),
                "pm25": _number(row.get("pm25")),
                "aqi": _number(row.get("aqi")),
                "category": row.get("category") or None,
                "aqi_source": row.get("aqi_source") or None,
                "reported_aqi": _number(row.get("reported_aqi")),
                "observed_at": _iso(row.get("observed_at")),
                "time_label": row.get("time_label") or "",
            }
        )
    return rows


def _iso(value: object) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        return value or None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: object) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    if number.is_integer():
        return int(number)
    return number
