"""OpenAQ v3 PM2.5 readings.

Locations in the box are listed first (`parameters_id=2` is PM2.5). The closest
few are then read from `/v3/locations/{id}/latest`, and only the PM2.5 sensor is
kept. One location failing does not drop the others.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pandas as pd

from wildfirebot.geo import BBox, haversine_km
from wildfirebot.http import HttpClient, HttpError, format_age
from wildfirebot.sources.airnow import OBS_COLUMNS, empty_observations
from wildfirebot.sources.base import SourceResult, TimeRange, optional_float

OPENAQ_BASE = "https://api.openaq.org/v3"
PM25_PARAMETER_ID = 2
MAX_LOCATIONS = 8


def openaq_body_ok(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("results"), list)


class OpenAQClient:
    name = "openaq"

    def __init__(
        self,
        api_key: str,
        http: HttpClient,
        *,
        ttl: timedelta = timedelta(minutes=30),
        refresh: bool = False,
        max_locations: int = MAX_LOCATIONS,
    ) -> None:
        self.api_key = api_key.strip()
        self.http = http
        self.ttl = ttl
        self.refresh = refresh
        self.max_locations = max_locations

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        if not self.api_key:
            return SourceResult(
                data=empty_observations(),
                notices=[
                    "OPENAQ_API_KEY is not set, so OpenAQ observations were not requested. "
                    "Create a key from https://docs.openaq.org/using-the-api/quick-start."
                ],
                degraded=True,
            )
        headers = {"X-API-Key": self.api_key}
        notices: list[str] = []
        stale = False
        try:
            listed = self._get(self._locations_url(bbox), headers)
        except HttpError as exc:
            return SourceResult(
                data=empty_observations(),
                notices=[f"OpenAQ location search failed ({exc})."],
                degraded=True,
                sources_failed=["openaq"],
            )
        stale = listed.stale
        if listed.stale and listed.stored_at is not None:
            notices.append(self._stale_notice("OpenAQ locations", listed.stored_at))
        locations = _parse_locations(json.loads(listed.text))
        if not locations:
            notices.append("OpenAQ has no PM2.5 locations in this box.")
            return SourceResult(
                data=empty_observations(),
                notices=notices,
                degraded=stale,
                stale=stale,
                sources_used=["openaq"],
            )
        center_lat, center_lon = bbox.centroid
        locations.sort(
            key=lambda item: haversine_km(
                center_lat, center_lon, item["latitude"], item["longitude"]
            )
        )
        if len(locations) > self.max_locations:
            notices.append(
                f"OpenAQ returned {len(locations)} PM2.5 locations; reading the closest {self.max_locations}."
            )
            locations = locations[: self.max_locations]

        rows: list[dict] = []
        failures = 0
        for location in locations:
            try:
                latest = self._get(
                    f"{OPENAQ_BASE}/locations/{location['id']}/latest?limit=100", headers
                )
            except HttpError as exc:
                failures += 1
                notices.append(
                    f"OpenAQ location {location['id']} ({location['name']}) failed ({exc})."
                )
                continue
            stale = stale or latest.stale
            if latest.stale and latest.stored_at is not None:
                notices.append(
                    self._stale_notice(f"OpenAQ location {location['id']}", latest.stored_at)
                )
            rows.extend(_readings_from_latest(json.loads(latest.text), location, time_range))

        if failures and not rows:
            notices.append(
                "OpenAQ locations were found, but none of their latest readings could be read."
            )
            return SourceResult(
                data=empty_observations(),
                notices=notices,
                degraded=True,
                stale=stale,
                sources_failed=["openaq"],
            )
        if not rows:
            notices.append(
                "OpenAQ readings were older than the requested window or were not PM2.5."
            )
        frame = pd.DataFrame(rows, columns=OBS_COLUMNS) if rows else empty_observations()
        if not frame.empty:
            frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        degraded = stale or failures > 0
        return SourceResult(
            data=frame,
            notices=notices,
            degraded=degraded,
            stale=stale,
            sources_used=["openaq"] if rows or not failures else [],
            sources_failed=["openaq"] if failures and not rows else [],
        )

    def _get(self, url: str, headers: dict[str, str]):
        return self.http.get_text(
            url,
            headers=headers,
            ttl=self.ttl,
            refresh=self.refresh,
            accept=openaq_body_ok,
        )

    def _locations_url(self, bbox: BBox) -> str:
        params = {
            "bbox": f"{bbox.west:.4f},{bbox.south:.4f},{bbox.east:.4f},{bbox.north:.4f}",
            "parameters_id": str(PM25_PARAMETER_ID),
            "limit": "100",
        }
        return f"{OPENAQ_BASE}/locations?{urlencode(params)}"

    def _stale_notice(self, label: str, stored_at: datetime) -> str:
        age = format_age(self.http.clock() - stored_at)
        minutes = max(1, int(self.ttl.total_seconds() // 60))
        return (
            f"{label}: the live feed did not respond. Using a cached response from {age} ago, "
            f"older than the {minutes} minute cache window."
        )


def _parse_locations(payload: dict) -> list[dict]:
    found = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        coordinates = item.get("coordinates") or {}
        latitude = optional_float(coordinates.get("latitude"))
        longitude = optional_float(coordinates.get("longitude"))
        if latitude is None or longitude is None:
            continue
        sensors = []
        for sensor in item.get("sensors") or []:
            if not isinstance(sensor, dict):
                continue
            parameter = sensor.get("parameter") or {}
            parameter_id = parameter.get(
                "id", sensor.get("parametersId", sensor.get("parameters_id"))
            )
            if optional_float(parameter_id) == PM25_PARAMETER_ID:
                sensor_id = sensor.get("id", sensor.get("sensorsId"))
                if sensor_id is not None:
                    sensors.append(int(sensor_id))
        if not sensors:
            continue
        found.append(
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or f"OpenAQ {item['id']}"),
                "latitude": latitude,
                "longitude": longitude,
                "sensor_ids": sensors,
            }
        )
    return found


def _readings_from_latest(payload: dict, location: dict, time_range: TimeRange) -> list[dict]:
    wanted = set(location["sensor_ids"])
    rows = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        sensor_id = item.get("sensorsId", item.get("sensors_id"))
        if sensor_id is None or int(sensor_id) not in wanted:
            continue
        value = optional_float(item.get("value"))
        if value is None:
            continue
        moment = _parse_utc((item.get("datetime") or {}).get("utc"))
        if moment is None or moment < time_range.start or moment > time_range.end:
            continue
        coordinates = item.get("coordinates") or {}
        latitude = optional_float(coordinates.get("latitude")) or location["latitude"]
        longitude = optional_float(coordinates.get("longitude")) or location["longitude"]
        rows.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "observed_at": moment,
                "time_label": "",
                "pm25": value,
                "reported_aqi": None,
                "site_name": location["name"],
                "source": "openaq",
            }
        )
    return rows


def _parse_utc(value: object) -> datetime | None:
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
