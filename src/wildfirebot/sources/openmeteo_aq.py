"""Open-Meteo air-quality forecast: hourly background PM2.5 for the plume model.

No API key. This is the CAMS-based regional forecast Open-Meteo serves at
`air-quality-api.open-meteo.com`. The plume forecaster adds fire smoke on top of
it, so it is the baseline, not the answer.
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

OPEN_METEO_AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
PM25_COLUMNS = ["time", "pm25"]


def build_air_quality_url(
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
        forecast_days = max(1, math.ceil((time_range.end - current).total_seconds() / 86400) + 1)
    params = {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "hourly": "pm2_5",
        "timezone": "UTC",
        "past_days": str(min(past_days, 92)),
        "forecast_days": str(min(forecast_days, 7)),
    }
    return f"{OPEN_METEO_AQ_URL}?{urlencode(params)}"


def air_quality_body_ok(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    hourly = payload.get("hourly") if isinstance(payload, dict) else None
    return isinstance(hourly, dict) and isinstance(hourly.get("time"), list)


def empty_pm25() -> pd.DataFrame:
    return pd.DataFrame(columns=PM25_COLUMNS)


def parse_air_quality(payload: dict) -> pd.DataFrame:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get("pm2_5") or []
    rows = []
    for index, raw_time in enumerate(times):
        if not isinstance(raw_time, str) or not raw_time:
            continue
        try:
            moment = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        value = optional_float(values[index]) if index < len(values) else None
        if value is None or value < 0:
            continue
        rows.append({"time": moment.astimezone(UTC), "pm25": value})
    if not rows:
        return empty_pm25()
    frame = pd.DataFrame(rows, columns=PM25_COLUMNS)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame


class OpenMeteoAirQualityClient:
    name = "open-meteo-air-quality"

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
        url = build_air_quality_url(latitude, longitude, time_range, now=current)
        try:
            payload = self.http.get_text(
                url, ttl=self.ttl, refresh=self.refresh, accept=air_quality_body_ok
            )
        except HttpError as exc:
            return SourceResult(
                data=empty_pm25(),
                notices=[f"Open-Meteo air-quality forecast is unavailable ({exc})."],
                degraded=True,
                sources_failed=[self.name],
            )
        try:
            frame = parse_air_quality(json.loads(payload.text))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            return SourceResult(
                data=empty_pm25(),
                notices=[
                    f"Open-Meteo returned an air-quality forecast that could not be read ({exc})."
                ],
                degraded=True,
                sources_failed=[self.name],
            )
        notices: list[str] = []
        if payload.stale and payload.stored_at is not None:
            age = format_age(self.http.clock() - payload.stored_at)
            notices.append(
                f"Open-Meteo air quality: the live forecast did not respond. Using a cached "
                f"forecast from {age} ago."
            )
        if not frame.empty:
            start = floor_hour(time_range.start)
            end = floor_hour(time_range.end)
            times = pd.to_datetime(frame["time"], utc=True)
            frame = frame.loc[(times >= start) & (times <= end)].reset_index(drop=True)
        if frame.empty:
            notices.append("Open-Meteo returned no PM2.5 hours inside the requested window.")
            return SourceResult(
                data=frame, notices=notices, degraded=True, sources_failed=[self.name]
            )
        return SourceResult(
            data=frame,
            notices=notices,
            degraded=payload.stale,
            stale=payload.stale,
            sources_used=[self.name],
        )
