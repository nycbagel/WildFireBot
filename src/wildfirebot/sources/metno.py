"""MET Norway Locationforecast: the backup source for the 10 m wind. No API key.

API: https://api.met.no/weatherapi/locationforecast/2.0/documentation
Terms: https://docs.api.met.no/doc/TermsOfService (to be read and accepted by a person
before using the service; the project owner did on 2026-11-21).

Open-Meteo stays the main wind feed because it also gives the 850 hPa wind and the
boundary-layer height. MET Norway is asked only when Open-Meteo has no wind, for
example when a shared host's outbound address is over Open-Meteo's free limit and
every request comes back HTTP 429. It covers the whole globe from the current hour
on: hourly for about two and a half days, then every six hours.

What this client does to keep MET Norway's terms:
- HttpClient sends a User-Agent that names the app and links to the repository, and
  accepts gzip.
- Coordinates are rounded to four decimals, so answers can be cached and reused.
- An answer is kept for 60 minutes, longer than its Expires header, and a refresh
  never skips that cache: the terms say never to repeat a request before it expires.
- A 429 is not retried. After one, the process stops asking MET Norway for 10
  minutes, or for as long as Retry-After says if that is longer.
- Answers that use this data credit MET Norway and link the CC BY 4.0 licence.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pandas as pd

from wildfirebot.geo import BBox
from wildfirebot.http import HttpClient, HttpError, HttpText, format_age
from wildfirebot.sources.base import SourceResult, TimeRange, floor_hour, optional_float
from wildfirebot.sources.openmeteo import WIND_COLUMNS, empty_winds

METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# a 5xx is worth another try; a 429 means "slow down", so it is not retried
METNO_RETRY_STATUSES = frozenset({500, 502, 503, 504})
THROTTLE_PAUSE = timedelta(minutes=10)

# The MCP server builds a new client for every tool call, so the pause after a 429 is
# kept for the whole process, not on one client.
_pause_lock = threading.Lock()
_paused_until: datetime | None = None


def paused_until() -> datetime | None:
    with _pause_lock:
        return _paused_until


def reset_pause() -> None:
    """Forget an earlier 429. For tests."""
    global _paused_until
    with _pause_lock:
        _paused_until = None


def _pause(until: datetime) -> None:
    global _paused_until
    with _pause_lock:
        if _paused_until is None or until > _paused_until:
            _paused_until = until


def build_metno_url(latitude: float, longitude: float) -> str:
    params = {"lat": f"{latitude:.4f}", "lon": f"{longitude:.4f}"}
    return f"{METNO_URL}?{urlencode(params)}"


def metno_body_ok(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    properties = payload.get("properties") if isinstance(payload, dict) else None
    return isinstance(properties, dict) and isinstance(properties.get("timeseries"), list)


def parse_metno(payload: dict) -> tuple[pd.DataFrame, list[str]]:
    """Read the 10 m wind out of a Locationforecast answer.

    The 850 hPa wind and the boundary-layer height are not in this product, so those
    columns stay empty and the transport step treats them as missing.
    """
    notices: list[str] = []
    properties = payload.get("properties") or {}
    units = (properties.get("meta") or {}).get("units") or {}
    scale = _speed_scale(units.get("wind_speed"))
    if scale is None:
        raise ValueError(f"unsupported MET Norway wind unit {units.get('wind_speed')}")
    rows = []
    for step in properties.get("timeseries") or []:
        if not isinstance(step, dict):
            continue
        moment = _parse_time(step.get("time"))
        if moment is None:
            continue
        details = ((step.get("data") or {}).get("instant") or {}).get("details") or {}
        speed = optional_float(details.get("wind_speed"))
        rows.append(
            {
                "time": moment,
                "wind_speed_10m": None if speed is None else speed * scale,
                "wind_direction_10m": optional_float(details.get("wind_from_direction")),
                "wind_speed_850hPa": None,
                "wind_direction_850hPa": None,
                "boundary_layer_height": None,
            }
        )
    if not rows:
        return empty_winds(), notices
    frame = pd.DataFrame(rows, columns=WIND_COLUMNS)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame, notices


class MetNorwayClient:
    name = "met-norway"

    def __init__(
        self,
        http: HttpClient,
        *,
        ttl: timedelta = timedelta(minutes=60),
        now: datetime | None = None,
    ) -> None:
        self.http = http
        self.ttl = ttl
        self._now = now

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        latitude, longitude = bbox.centroid
        url = build_metno_url(latitude, longitude)
        clock = self.http.clock()
        pause = paused_until()
        if pause is not None and clock < pause:
            payload = self._cached(url, clock)
            if payload is None:
                return _failed(
                    "MET Norway asked WildFireBot to slow down (HTTP 429), so it is not "
                    f"asked again until {pause:%H:%M} UTC."
                )
        else:
            try:
                # refresh is always False: MET Norway asks never to repeat a request early
                payload = self.http.get_text(
                    url,
                    ttl=self.ttl,
                    refresh=False,
                    accept=metno_body_ok,
                    retry_statuses=METNO_RETRY_STATUSES,
                )
            except HttpError as exc:
                if exc.status == 429:
                    wait = max(THROTTLE_PAUSE, timedelta(seconds=exc.retry_after or 0))
                    _pause(clock + wait)
                return _failed(f"MET Norway wind forecast is unavailable ({exc}).")
        try:
            frame, notices = parse_metno(json.loads(payload.text))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return _failed(f"MET Norway returned a wind forecast that could not be read ({exc}).")
        if payload.stale and payload.stored_at is not None:
            age = format_age(clock - payload.stored_at)
            notices.append(f"MET Norway did not answer. Using its forecast from {age} ago.")
        frame = _clip(frame, time_range)
        if frame.empty:
            notices.append("MET Norway returned no wind hours inside the requested window.")
            return SourceResult(
                data=frame,
                notices=notices,
                degraded=True,
                stale=payload.stale,
                sources_failed=["met-norway"],
            )
        frame, held, first = _hold_first_wind_back(frame, time_range)
        if held:
            notices.append(
                f"MET Norway's forecast starts at {first:%H:%M} UTC, so the {held} hour(s) "
                "before it assume that hour's wind held steady."
            )
        return SourceResult(
            data=frame,
            notices=notices,
            degraded=payload.stale,
            stale=payload.stale,
            sources_used=["met-norway"],
        )

    def _cached(self, url: str, clock: datetime) -> HttpText | None:
        """While paused, answer from the cache only, however old, and never ask again."""
        cached = self.http.cache.get(url) if self.http.cache is not None else None
        if cached is None or not metno_body_ok(cached[0]):
            return None
        body, stored_at = cached
        return HttpText(body, True, clock - stored_at > self.ttl, stored_at, url)


def _failed(notice: str) -> SourceResult:
    return SourceResult(
        data=empty_winds(), notices=[notice], degraded=True, sources_failed=["met-norway"]
    )


def _hold_first_wind_back(
    frame: pd.DataFrame, time_range: TimeRange
) -> tuple[pd.DataFrame, int, datetime | None]:
    """MET Norway has no past hours: its forecast starts at the current hour.

    The hourly smoke score follows the wind back up to 12 hours, so without earlier
    hours its first forecast hours would stop short and understate smoke from fires
    more than an hour or two upwind. Those hours take the first forecast wind instead.
    """
    start = pd.Timestamp(floor_hour(time_range.start))
    times = pd.to_datetime(frame["time"], utc=True)
    first = times.min()
    if first <= start:
        return frame, 0, None
    valid = frame.loc[frame["wind_speed_10m"].notna() & frame["wind_direction_10m"].notna()]
    if valid.empty:
        return frame, 0, None
    seed = valid.assign(time=pd.to_datetime(valid["time"], utc=True)).sort_values("time").iloc[0]
    hours = pd.date_range(start, first - pd.Timedelta(hours=1), freq="h")
    earlier = pd.DataFrame(
        {
            "time": hours,
            "wind_speed_10m": seed["wind_speed_10m"],
            "wind_direction_10m": seed["wind_direction_10m"],
            "wind_speed_850hPa": None,
            "wind_direction_850hPa": None,
            "boundary_layer_height": None,
        },
        columns=WIND_COLUMNS,
    )
    merged = pd.concat([earlier, frame], ignore_index=True)
    return merged, len(earlier), first.to_pydatetime()


def _clip(frame: pd.DataFrame, time_range: TimeRange) -> pd.DataFrame:
    if frame.empty:
        return frame
    times = pd.to_datetime(frame["time"], utc=True)
    mask = (times >= floor_hour(time_range.start)) & (times <= floor_hour(time_range.end))
    return frame.loc[mask].reset_index(drop=True)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _speed_scale(unit: object) -> float | None:
    if unit is None or unit == "":
        return 1.0
    normalized = str(unit).lower().replace(" ", "")
    return {"m/s": 1.0, "ms": 1.0, "km/h": 1.0 / 3.6, "kmh": 1.0 / 3.6, "kn": 0.514444}.get(
        normalized
    )
