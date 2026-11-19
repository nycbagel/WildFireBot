"""Tool handlers the CLI uses today and an MCP server will expose later.

The Model Context Protocol server is milestone 3. It should be a thin stdio
adapter over `TOOLS` and `call_tool`. Forecast math stays in the pipeline so the
human CLI and the agent server cannot drift apart.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from wildfirebot.http import FileCache, FixtureTransport, HttpClient, UrllibTransport
from wildfirebot.model.aqi import pm25_to_aqi
from wildfirebot.pipeline import ForecastReport, build_report
from wildfirebot.sources.airnow import AirNowClient
from wildfirebot.sources.firms import DEFAULT_SOURCES, KNOWN_SOURCES, FirmsClient, parse_sources
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient

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
