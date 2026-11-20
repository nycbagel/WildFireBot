"""AirNow PM2.5 observations.

The hourly bounding-box feed (`/aq/data/`) is the primary request. If it errors,
the client asks for the current observation by latitude and longitude through
`/aq/observation/current/ziplatlong/`. AirNow retired the older
`/aq/observation/latLong/current/` service on October 1, 2026 (it answers 410).
The new service uses camelCase fields, reports a NowCast AQI without a
concentration, and omits site coordinates, so those rows take the request point. Concentrations are preferred. A response that only has an AQI is kept
and labeled as a reported AQI rather than converted backwards into a concentration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pandas as pd

from wildfirebot.geo import BBox
from wildfirebot.http import HttpClient, HttpError, format_age
from wildfirebot.sources.base import SourceResult, TimeRange, optional_float

AIRNOW_DATA_URL = "https://www.airnowapi.org/aq/data/"
AIRNOW_CURRENT_URL = "https://www.airnowapi.org/aq/observation/current/ziplatlong/"

OBS_COLUMNS = [
    "latitude",
    "longitude",
    "observed_at",
    "time_label",
    "pm25",
    "reported_aqi",
    "site_name",
    "source",
]


def airnow_body_ok(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    if isinstance(payload, dict):
        return False
    if not isinstance(payload, list):
        return False
    if payload and isinstance(payload[0], dict):
        row = payload[0]
        if "Message" in row and "Latitude" not in row and "latitude" not in row:
            return False
    return True


def empty_observations() -> pd.DataFrame:
    return pd.DataFrame(columns=OBS_COLUMNS)


def parse_airnow(
    payload: list,
    *,
    source_label: str = "airnow",
    default_location: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """Read either AirNow schema. `default_location` fills rows that carry no coordinates."""
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        parameter = str(
            item.get("Parameter") or item.get("ParameterName") or item.get("parameterName") or ""
        )
        if parameter and "PM25" not in parameter.upper().replace(".", "").replace(" ", ""):
            continue
        latitude = optional_float(item.get("Latitude", item.get("latitude")))
        longitude = optional_float(item.get("Longitude", item.get("longitude")))
        if (latitude is None or longitude is None) and default_location is not None:
            latitude, longitude = default_location
        if latitude is None or longitude is None:
            continue
        concentration = _first_float(item, "RawConcentration", "Value", "Concentration", "conc")
        if concentration is not None and concentration < 0:
            concentration = None
        reported = _first_float(item, "AQI", "Aqi", "nowcastAQI", "aqi")
        if reported is not None and reported < 0:
            reported = None
        observed_at, time_label = _airnow_time(item)
        if concentration is None and reported is None:
            continue
        site = (
            item.get("SiteName")
            or item.get("siteName")
            or item.get("ReportingArea")
            or item.get("reportingAreaName")
            or item.get("Site")
            or ""
        )
        rows.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "observed_at": observed_at,
                "time_label": time_label,
                "pm25": concentration,
                "reported_aqi": reported,
                "site_name": str(site),
                "source": source_label,
            }
        )
    if not rows:
        return empty_observations()
    frame = pd.DataFrame(rows, columns=OBS_COLUMNS)
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True, errors="coerce")
    return frame


class AirNowClient:
    name = "airnow"

    def __init__(
        self,
        api_key: str,
        http: HttpClient,
        *,
        ttl: timedelta = timedelta(minutes=30),
        refresh: bool = False,
    ) -> None:
        self.api_key = api_key.strip()
        self.http = http
        self.ttl = ttl
        self.refresh = refresh

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        if not self.api_key:
            return SourceResult(
                data=empty_observations(),
                notices=[
                    "AIRNOW_API_KEY is not set, so AirNow observations were not requested. "
                    "Request a key at https://docs.airnowapi.org/account/request/."
                ],
                degraded=True,
            )
        notices: list[str] = []
        stale = False
        try:
            hourly = self._get(self._hourly_url(bbox, time_range))
        except HttpError as exc:
            hourly = None
            notices.append(f"AirNow hourly box feed failed ({exc}).")
        if hourly is not None:
            stale = stale or hourly.stale
            if hourly.stale and hourly.stored_at is not None:
                notices.append(self._stale_notice("AirNow hourly observations", hourly.stored_at))
            frame = parse_airnow(json.loads(hourly.text))
            frame = _within_window(frame, time_range)
            if not frame.empty:
                return SourceResult(
                    data=frame,
                    notices=notices,
                    degraded=stale or bool(notices),
                    stale=stale,
                    sources_used=["airnow"],
                )
            notices.append("AirNow hourly box feed returned no PM2.5 rows in this window.")

        try:
            current = self._get(self._current_url(bbox))
        except HttpError as exc:
            notices.append(f"AirNow current lat/lon observation also failed ({exc}).")
            return SourceResult(
                data=empty_observations(),
                notices=notices,
                degraded=True,
                sources_failed=["airnow"],
            )
        stale = stale or current.stale
        if current.stale and current.stored_at is not None:
            notices.append(self._stale_notice("AirNow current observation", current.stored_at))
        frame = parse_airnow(json.loads(current.text), default_location=bbox.centroid)
        if frame.empty:
            notices.append("AirNow has no current PM2.5 observation near the box center.")
            return SourceResult(
                data=frame,
                notices=notices,
                degraded=True,
                stale=stale,
                sources_used=["airnow"],
            )
        notices.append(
            "Showing the current AirNow reporting-area observation because the hourly box feed "
            "had no usable PM2.5 rows."
        )
        return SourceResult(
            data=frame,
            notices=notices,
            degraded=True,
            stale=stale,
            sources_used=["airnow"],
        )

    def _get(self, url: str):
        return self.http.get_text(url, ttl=self.ttl, refresh=self.refresh, accept=airnow_body_ok)

    def _hourly_url(self, bbox: BBox, time_range: TimeRange) -> str:
        params = {
            "startDate": time_range.start.astimezone(UTC).strftime("%Y-%m-%dT%H"),
            "endDate": time_range.end.astimezone(UTC).strftime("%Y-%m-%dT%H"),
            "parameters": "PM25",
            "BBOX": f"{bbox.west:.4f},{bbox.south:.4f},{bbox.east:.4f},{bbox.north:.4f}",
            "dataType": "B",
            "format": "application/json",
            "verbose": "1",
            "monitorType": "0",
            "includerawconcentrations": "1",
            "API_KEY": self.api_key,
        }
        return f"{AIRNOW_DATA_URL}?{urlencode(params)}"

    def _current_url(self, bbox: BBox) -> str:
        latitude, longitude = bbox.centroid
        params = {
            "format": "application/json",
            "latitude": f"{latitude:.4f}",
            "longitude": f"{longitude:.4f}",
            "distance": "25",
            "API_KEY": self.api_key,
        }
        return f"{AIRNOW_CURRENT_URL}?{urlencode(params)}"

    def _stale_notice(self, label: str, stored_at: datetime) -> str:
        age = format_age(self.http.clock() - stored_at)
        minutes = max(1, int(self.ttl.total_seconds() // 60))
        return (
            f"{label}: the live feed did not respond. Using a cached response from {age} ago, "
            f"older than the {minutes} minute cache window."
        )


def _first_float(item: dict, *keys: str) -> float | None:
    for key in keys:
        if key in item and item[key] not in (None, ""):
            number = optional_float(item[key])
            if number is not None:
                return number
    return None


def _airnow_time(item: dict) -> tuple[datetime | None, str]:
    raw = item.get("UTC") or item.get("Utc")
    if isinstance(raw, str) and raw:
        text = raw.replace("Z", "+00:00")
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC), ""
    date_text = item.get("DateObserved") or item.get("dateObserved")
    hour = item.get("HourObserved", item.get("hourObserved"))
    zone = item.get("LocalTimeZone") or item.get("localTimeZone") or "local"
    if date_text:
        hour_text = _hour_text(hour)
        return None, f"{date_text} {hour_text} {zone}".strip()
    return None, ""


def _hour_text(hour: object) -> str:
    """Old schema sends 12, the ziplatlong schema sends "15:00"."""
    if hour in (None, ""):
        return ""
    text = str(hour).strip()
    head = text.split(":", 1)[0]
    try:
        return f"{int(head):02d}:00"
    except ValueError:
        return text


def _within_window(frame: pd.DataFrame, time_range: TimeRange) -> pd.DataFrame:
    if frame.empty or frame["observed_at"].isna().all():
        return frame
    times = pd.to_datetime(frame["observed_at"], utc=True)
    mask = times.isna() | ((times >= time_range.start) & (times <= time_range.end))
    return frame.loc[mask].reset_index(drop=True)
