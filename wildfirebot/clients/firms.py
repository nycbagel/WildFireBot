"""NASA FIRMS area API client.

Endpoint, for the most recent day::

    https://firms.modaps.eosdis.nasa.gov/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/1

``SOURCE`` may be VIIRS_NOAA20_NRT (the default), VIIRS_NOAA21_NRT,
VIIRS_SNPP_NRT, or MODIS_NRT. VIIRS confidence is ``l``, ``n``, or ``h``.
MODIS confidence is a number from 0 to 100. Both are returned as text.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from wildfirebot.clients.http import ClientError, Transport, get_text, redact
from wildfirebot.geo import bounding_boxes, format_box

FIRMS_AREA_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
ALLOWED_SOURCES = (
    "VIIRS_NOAA20_NRT",
    "VIIRS_NOAA21_NRT",
    "VIIRS_SNPP_NRT",
    "MODIS_NRT",
)
DAY_RANGE = 1


@dataclass(frozen=True)
class Hotspot:
    """One active-fire pixel. Field names follow the FIRMS CSV."""

    lat: float
    lon: float
    frp: float
    confidence: str
    acq_date: str
    acq_time: str

    def as_dict(self) -> dict[str, object]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "frp": self.frp,
            "confidence": self.confidence,
            "acq_date": self.acq_date,
            "acq_time": self.acq_time,
        }


@dataclass(frozen=True)
class FirmsResult:
    status: str
    detail: str
    source: str
    hotspots: tuple[Hotspot, ...]


def _error(source: str, detail: str) -> FirmsResult:
    return FirmsResult(status="error", detail=detail, source=source, hotspots=())


def normalize_acq_time(value: str) -> str:
    """Return FIRMS ``acq_time`` as four digits (``1830``) when it is numeric.

    The CSV stores UTC time as HMM or HHMM. ``1`` means 00:01, ``1830`` means 18:30.
    """

    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if re.fullmatch(r"\d{1,4}", text):
        return text.zfill(4)
    return text


def _parse_csv(text: str) -> list[Hotspot]:
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        raise ClientError("FIRMS returned an empty body")
    reader = csv.DictReader(io.StringIO(stripped))
    if not reader.fieldnames:
        raise ClientError("FIRMS response had no CSV header")
    reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    if "latitude" not in reader.fieldnames or "longitude" not in reader.fieldnames:
        snippet = redact(" ".join(stripped.split()), [])[:180]
        raise ClientError(f"FIRMS response was not an active-fire CSV ({snippet})")
    hotspots: list[Hotspot] = []
    for row in reader:
        try:
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            frp = float(row["frp"])
        except (KeyError, TypeError, ValueError):
            continue
        if frp < 0:
            continue
        confidence = str(row.get("confidence") or "").strip()
        acq_date = str(row.get("acq_date") or "").strip()
        acq_time = normalize_acq_time(str(row.get("acq_time") or "").strip())
        hotspots.append(
            Hotspot(
                lat=lat,
                lon=lon,
                frp=frp,
                confidence=confidence,
                acq_date=acq_date,
                acq_time=acq_time,
            )
        )
    return hotspots


def fetch_hotspots(
    lat: float,
    lon: float,
    radius_km: float,
    *,
    map_key: str,
    source: str = "VIIRS_NOAA20_NRT",
    user_agent: str,
    transport: Transport | None = None,
) -> FirmsResult:
    """Download the last day of detections in a box around the location.

    The box can be larger than the circle. Callers filter with a great-circle
    distance. A missing key skips the source instead of failing the outlook.
    """

    if source not in ALLOWED_SOURCES:
        allowed = ", ".join(ALLOWED_SOURCES)
        raise ValueError(f"FIRMS source must be one of {allowed}")
    if not map_key:
        return FirmsResult(
            status="skipped",
            detail="FIRMS_MAP_KEY is not set, so NASA FIRMS detections were skipped.",
            source=source,
            hotspots=(),
        )
    headers = {"User-Agent": user_agent, "Accept": "text/csv, */*"}
    collected: list[Hotspot] = []
    try:
        for box in bounding_boxes(lat, lon, radius_km):
            area = format_box(*box)
            url = f"{FIRMS_AREA_URL}/{map_key}/{source}/{area}/{DAY_RANGE}"
            try:
                status, body = get_text(url, headers=headers, transport=transport)
            except ClientError as exc:
                return _error(source, redact(str(exc), [map_key]))
            except Exception as exc:
                return _error(source, f"FIRMS request failed ({exc.__class__.__name__})")
            if status != 200:
                return _error(source, f"FIRMS request failed (HTTP {status})")
            collected.extend(_parse_csv(body))
    except ClientError as exc:
        return _error(source, redact(str(exc), [map_key]))
    deduped = _dedupe(collected)
    return FirmsResult(
        status="ok",
        detail=f"{len(deduped)} FIRMS {source} detections in the search box.",
        source=source,
        hotspots=tuple(deduped),
    )


def _dedupe(hotspots: list[Hotspot]) -> list[Hotspot]:
    """Keep the strongest FRP when the same pixel is returned twice (dateline split)."""

    best: dict[tuple[float, float, str, str], Hotspot] = {}
    for hotspot in hotspots:
        key = (round(hotspot.lat, 5), round(hotspot.lon, 5), hotspot.acq_date, hotspot.acq_time)
        current = best.get(key)
        if current is None or hotspot.frp > current.frp:
            best[key] = hotspot
    return list(best.values())
