"""Open-Meteo air-quality forecast. No API key.

``https://air-quality-api.open-meteo.com/v1/air-quality`` with
``hourly=pm2_5`` is the baseline PM2.5 forecast, and the fallback when
AirNow has no current concentration. ``timezone=UTC`` and ``forecast_days=3``
are added so a 48-hour horizon has timestamps that line up with the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from wildfirebot.clients.http import ClientError, Transport, get_text, parse_json
from wildfirebot.geo import format_coord

OPEN_METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"


@dataclass(frozen=True)
class OpenMeteoResult:
    status: str
    detail: str
    series: tuple[tuple[datetime, float], ...]

    def at(self, when: datetime) -> float | None:
        """PM2.5 for the latest hour at or before ``when``.

        Hours after the end of the series use the last value. A target before
        the series uses the first value. Missing hours are already dropped.
        """

        if not self.series:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        chosen: float | None = None
        for stamp, value in self.series:
            if stamp <= when:
                chosen = value
            else:
                break
        if chosen is None:
            return self.series[0][1]
        return chosen


def _parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _series(payload: object) -> list[tuple[datetime, float]]:
    if not isinstance(payload, dict):
        raise ClientError("Open-Meteo response was not a JSON object")
    reason = payload.get("reason") or payload.get("error")
    if reason and "hourly" not in payload:
        raise ClientError(str(reason))
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        raise ClientError("Open-Meteo response had no hourly block")
    times = hourly.get("time")
    values = hourly.get("pm2_5")
    if not isinstance(times, list) or not isinstance(values, list):
        raise ClientError("Open-Meteo response had no hourly pm2_5 series")
    series: list[tuple[datetime, float]] = []
    for stamp, value in zip(times, values):
        if value is None or stamp is None:
            continue
        try:
            series.append((_parse_time(str(stamp)), float(value)))
        except (TypeError, ValueError):
            continue
    series.sort(key=lambda item: item[0])
    return series


def fetch_pm25_forecast(
    lat: float,
    lon: float,
    *,
    user_agent: str,
    transport: Transport | None = None,
) -> OpenMeteoResult:
    """Hourly PM2.5 (µg/m³) for the next three days, in UTC."""

    query = urlencode(
        {
            "latitude": format_coord(lat),
            "longitude": format_coord(lon),
            "hourly": "pm2_5",
            "timezone": "UTC",
            "forecast_days": "3",
        }
    )
    url = f"{OPEN_METEO_URL}?{query}"
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    try:
        status, body = get_text(url, headers=headers, transport=transport)
    except ClientError as exc:
        return OpenMeteoResult(status="error", detail=str(exc), series=())
    except Exception as exc:
        return OpenMeteoResult(
            status="error",
            detail=f"Open-Meteo request failed ({exc.__class__.__name__})",
            series=(),
        )
    if status != 200:
        return OpenMeteoResult(
            status="error",
            detail=f"Open-Meteo request failed (HTTP {status})",
            series=(),
        )
    try:
        series = _series(parse_json(body))
    except ClientError as exc:
        return OpenMeteoResult(status="error", detail=str(exc), series=())
    if not series:
        return OpenMeteoResult(
            status="error",
            detail="Open-Meteo returned no PM2.5 values.",
            series=(),
        )
    return OpenMeteoResult(
        status="ok",
        detail=f"Open-Meteo hourly PM2.5, {len(series)} hours.",
        series=tuple(series),
    )
