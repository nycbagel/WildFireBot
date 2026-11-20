"""National Weather Service hourly wind at one point.

Two calls, both with a User-Agent:

1. ``https://api.weather.gov/points/{lat},{lon}``
2. The ``forecastHourly`` URL from that response.

``windDirection`` is the direction the wind comes FROM (``W``, ``SW``, ...).
``windSpeed`` is text such as ``10 mph`` or ``5 to 15 mph``. A range uses the
midpoint. A missing unit is read as mph, which is what this endpoint sends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from wildfirebot.clients.http import ClientError, Transport, get_text, parse_json
from wildfirebot.geo import format_coord

NWS_POINTS_URL = "https://api.weather.gov/points"

# Direction the wind comes FROM, in degrees clockwise from north.
_COMPASS_FROM = {
    "N": 0.0,
    "NNE": 22.5,
    "NE": 45.0,
    "ENE": 67.5,
    "E": 90.0,
    "ESE": 112.5,
    "SE": 135.0,
    "SSE": 157.5,
    "S": 180.0,
    "SSW": 202.5,
    "SW": 225.0,
    "WSW": 247.5,
    "W": 270.0,
    "WNW": 292.5,
    "NW": 315.0,
    "NNW": 337.5,
    "NORTH": 0.0,
    "NORTHEAST": 45.0,
    "EAST": 90.0,
    "SOUTHEAST": 135.0,
    "SOUTH": 180.0,
    "SOUTHWEST": 225.0,
    "WEST": 270.0,
    "NORTHWEST": 315.0,
}

_SPEED_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)"
    r"(?:\s*(?:to|-)\s*(?P<high>\d+(?:\.\d+)?))?"
    r"\s*(?P<unit>mph|m/s|mps|km/h|kph|kt|knots)?",
    re.IGNORECASE,
)

_MPH_TO_M_S = 0.44704
_KNOT_TO_M_S = 0.514444


@dataclass(frozen=True)
class HourlyWind:
    start: datetime
    end: datetime
    speed_m_s: float
    from_deg: float
    speed_text: str
    direction_text: str

    @property
    def label(self) -> str:
        return f"from {self.direction_text} at {self.speed_text}"


@dataclass(frozen=True)
class NwsResult:
    status: str
    detail: str
    periods: tuple[HourlyWind, ...]

    def at(self, when: datetime) -> HourlyWind | None:
        """Period that covers ``when``, otherwise the period with the nearest start."""

        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        covering = [period for period in self.periods if period.start <= when < period.end]
        if covering:
            return covering[0]
        if not self.periods:
            return None
        return min(self.periods, key=lambda period: abs(period.start - when))


def parse_wind_from_deg(text: str) -> float:
    """Parse an NWS compass point into the direction the wind comes from."""

    token = re.sub(r"[\s-]+", "", str(text).strip().upper())
    try:
        return _COMPASS_FROM[token]
    except KeyError as exc:
        raise ValueError(f"unrecognized wind direction {text!r}") from exc


def parse_wind_speed_m_s(text: str) -> float:
    """Parse an NWS wind-speed string into meters per second.

    ``5 to 15 mph`` is the midpoint, 10 mph. ``Calm`` is 0. A missing unit is
    treated as mph.
    """

    raw = str(text).strip()
    if raw.lower() in {"calm", "0", "0 mph"}:
        return 0.0
    match = _SPEED_RE.search(raw)
    if match is None:
        raise ValueError(f"unrecognized wind speed {text!r}")
    low = float(match.group("low"))
    high = match.group("high")
    speed = (low + float(high)) / 2.0 if high is not None else low
    unit = (match.group("unit") or "mph").lower()
    if unit == "mph":
        return speed * _MPH_TO_M_S
    if unit in {"m/s", "mps"}:
        return speed
    if unit in {"km/h", "kph"}:
        return speed / 3.6
    if unit in {"kt", "knots"}:
        return speed * _KNOT_TO_M_S
    raise ValueError(f"unrecognized wind speed unit in {text!r}")


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _periods_from_payload(payload: object) -> list[HourlyWind]:
    if not isinstance(payload, dict):
        raise ClientError("hourly forecast was not a JSON object")
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise ClientError("hourly forecast had no properties")
    raw_periods = properties.get("periods")
    if not isinstance(raw_periods, list):
        raise ClientError("hourly forecast had no periods")
    periods: list[HourlyWind] = []
    for raw in raw_periods:
        if not isinstance(raw, dict):
            continue
        speed_text = str(raw.get("windSpeed") or "").strip()
        direction_text = str(raw.get("windDirection") or "").strip()
        start_text = raw.get("startTime")
        end_text = raw.get("endTime")
        if not speed_text or not direction_text or not start_text or not end_text:
            continue
        try:
            periods.append(
                HourlyWind(
                    start=_parse_time(str(start_text)),
                    end=_parse_time(str(end_text)),
                    speed_m_s=parse_wind_speed_m_s(speed_text),
                    from_deg=parse_wind_from_deg(direction_text),
                    speed_text=speed_text,
                    direction_text=direction_text.upper(),
                )
            )
        except ValueError:
            continue
    return periods


def fetch_hourly_winds(
    lat: float,
    lon: float,
    *,
    user_agent: str,
    transport: Transport | None = None,
) -> NwsResult:
    """Return parsed hourly winds for the grid cell that contains the point."""

    headers = {
        "User-Agent": user_agent,
        "Accept": "application/geo+json, application/json",
    }
    points_url = f"{NWS_POINTS_URL}/{format_coord(lat)},{format_coord(lon)}"
    try:
        status, body = get_text(points_url, headers=headers, transport=transport)
        if status != 200:
            return NwsResult(status="error", detail=f"NWS points request failed (HTTP {status})", periods=())
        payload = parse_json(body)
        hourly_url = _hourly_url(payload)
        status, body = get_text(hourly_url, headers=headers, transport=transport)
        if status != 200:
            return NwsResult(
                status="error",
                detail=f"NWS hourly forecast request failed (HTTP {status})",
                periods=(),
            )
        periods = _periods_from_payload(parse_json(body))
    except ClientError as exc:
        return NwsResult(status="error", detail=str(exc), periods=())
    except Exception as exc:
        return NwsResult(
            status="error",
            detail=f"NWS request failed ({exc.__class__.__name__})",
            periods=(),
        )
    if not periods:
        return NwsResult(
            status="error",
            detail="NWS hourly forecast did not include a usable wind speed and direction.",
            periods=(),
        )
    return NwsResult(
        status="ok",
        detail=f"{len(periods)} hourly wind periods from api.weather.gov.",
        periods=tuple(periods),
    )


def _hourly_url(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ClientError("NWS points response was not a JSON object")
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise ClientError("NWS points response had no properties")
    hourly = properties.get("forecastHourly")
    if not isinstance(hourly, str) or not hourly:
        raise ClientError("NWS points response had no hourly forecast link")
    parsed = urlparse(hourly)
    if parsed.scheme != "https" or parsed.netloc != "api.weather.gov":
        raise ClientError("hourly forecast link was not on api.weather.gov")
    return hourly
