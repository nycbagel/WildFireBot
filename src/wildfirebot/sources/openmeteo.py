"""Open-Meteo forecast winds at 10 m and 850 hPa, plus boundary-layer height.

No API key. The forecast is requested in m/s and UTC. If the payload declares a
different wind unit, speeds are converted. Hours are not dropped when a value is
null; the transport step interpolates short gaps and flags them.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pandas as pd

from wildfirebot.geo import BBox
from wildfirebot.http import HttpClient, HttpError, format_age
from wildfirebot.sources.base import SourceResult, TimeRange, floor_hour, optional_float

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_VARIABLES = (
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_speed_850hPa",
    "wind_direction_850hPa",
    "boundary_layer_height",
)
WIND_COLUMNS = [
    "time",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_speed_850hPa",
    "wind_direction_850hPa",
    "boundary_layer_height",
]


def build_openmeteo_url(
    latitude: float,
    longitude: float,
    time_range: TimeRange,
    *,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(UTC)
    past_days = 1
    if time_range.start < current:
        past_days = max(1, math.ceil((current - time_range.start).total_seconds() / 86400))
    forecast_days = 1
    if time_range.end > current:
        forecast_days = max(1, math.ceil((time_range.end - current).total_seconds() / 86400))
    params = {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "hourly": ",".join(HOURLY_VARIABLES),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "past_days": str(min(past_days, 92)),
        "forecast_days": str(min(forecast_days, 16)),
    }
    return f"{OPEN_METEO_URL}?{urlencode(params)}"


def openmeteo_body_ok(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    return isinstance(hourly, dict) and isinstance(hourly.get("time"), list)


def empty_winds() -> pd.DataFrame:
    return pd.DataFrame(columns=WIND_COLUMNS)


def parse_openmeteo(payload: dict) -> tuple[pd.DataFrame, list[str]]:
    notices: list[str] = []
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    units = payload.get("hourly_units") or {}
    surface_scale, surface_note = _speed_scale(units.get("wind_speed_10m"), "10 m")
    aloft_scale, aloft_note = _speed_scale(units.get("wind_speed_850hPa"), "850 hPa")
    notices.extend(note for note in (surface_note, aloft_note) if note)

    rows = []
    for index, raw_time in enumerate(times):
        moment = _parse_time(raw_time)
        if moment is None:
            continue
        rows.append(
            {
                "time": moment,
                "wind_speed_10m": _scaled(hourly.get("wind_speed_10m"), index, surface_scale),
                "wind_direction_10m": _at(hourly.get("wind_direction_10m"), index),
                "wind_speed_850hPa": _scaled(hourly.get("wind_speed_850hPa"), index, aloft_scale),
                "wind_direction_850hPa": _at(hourly.get("wind_direction_850hPa"), index),
                "boundary_layer_height": _at(hourly.get("boundary_layer_height"), index),
            }
        )
    if not rows:
        return empty_winds(), notices
    frame = pd.DataFrame(rows, columns=WIND_COLUMNS)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame, notices


class OpenMeteoClient:
    name = "open-meteo"

    def __init__(
        self,
        http: HttpClient,
        *,
        ttl: timedelta = timedelta(minutes=60),
        refresh: bool = False,
        now: datetime | None = None,
    ) -> None:
        self.http = http
        self.ttl = ttl
        self.refresh = refresh
        self._now = now

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        latitude, longitude = bbox.centroid
        current = self._now or datetime.now(UTC)
        url = build_openmeteo_url(latitude, longitude, time_range, now=current)
        try:
            payload = self.http.get_text(
                url,
                ttl=self.ttl,
                refresh=self.refresh,
                accept=openmeteo_body_ok,
            )
        except HttpError as exc:
            return SourceResult(
                data=empty_winds(),
                notices=[
                    f"Open-Meteo wind forecast is unavailable ({exc}). "
                    "Smoke scores that need wind are omitted."
                ],
                degraded=True,
                sources_failed=["open-meteo"],
            )
        try:
            body = json.loads(payload.text)
            frame, notices = parse_openmeteo(body)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            return SourceResult(
                data=empty_winds(),
                notices=[f"Open-Meteo returned a wind forecast that could not be read ({exc})."],
                degraded=True,
                sources_failed=["open-meteo"],
            )
        if payload.stale and payload.stored_at is not None:
            age = format_age(self.http.clock() - payload.stored_at)
            minutes = max(1, int(self.ttl.total_seconds() // 60))
            notices.append(
                f"Open-Meteo: the live forecast did not respond. Using cached winds from {age} ago, "
                f"older than the {minutes} minute cache window."
            )
        frame = _clip(frame, time_range)
        if frame.empty:
            notices.append("Open-Meteo returned no wind hours inside the requested window.")
            return SourceResult(
                data=frame,
                notices=notices,
                degraded=True,
                stale=payload.stale,
                sources_failed=["open-meteo"],
            )
        return SourceResult(
            data=frame,
            notices=notices,
            degraded=payload.stale,
            stale=payload.stale,
            sources_used=["open-meteo"],
        )


def _clip(frame: pd.DataFrame, time_range: TimeRange) -> pd.DataFrame:
    if frame.empty:
        return frame
    start = floor_hour(time_range.start)
    end = floor_hour(time_range.end)
    times = pd.to_datetime(frame["time"], utc=True)
    mask = (times >= start) & (times <= end)
    return frame.loc[mask].reset_index(drop=True)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _at(values: object, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    return optional_float(values[index])


def _scaled(values: object, index: int, scale: float) -> float | None:
    number = _at(values, index)
    if number is None:
        return None
    return number * scale


def _speed_scale(unit: object, label: str) -> tuple[float, str | None]:
    if unit is None or unit == "":
        return (
            1.0,
            f"Open-Meteo did not declare the {label} wind unit; speeds are assumed to be m/s.",
        )
    normalized = str(unit).lower().replace(" ", "")
    if normalized in {"m/s", "ms"}:
        return 1.0, None
    if normalized in {"km/h", "kmh"}:
        return 1.0 / 3.6, None
    if normalized == "mph":
        return 0.44704, None
    if normalized in {"kn", "knots"}:
        return 0.514444, None
    raise ValueError(f"unsupported {label} wind unit {unit}")
