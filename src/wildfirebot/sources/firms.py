"""NASA FIRMS area API client.

The area endpoint is
`/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/{days}/{date}`
and each call covers 1 to 5 days. NRT products already include real-time and
ultra-real-time detections; the `version` field says which processing level a
row came from, and URT rows are removed once the matching NRT row arrives or
after about six hours.

Default order prefers the newest VIIRS instrument, then older VIIRS, MODIS, and
Landsat. A source that fails is skipped. A later source adds a hotspot only when
it is outside `separation_km` of a hotspot already kept, so FRP from two
satellites is not added for the same flame.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from wildfirebot.fires.qc import filter_detections, format_drop_counts
from wildfirebot.geo import BBox, haversine_km
from wildfirebot.http import HttpClient, HttpError, format_age
from wildfirebot.sources.base import SourceResult, TimeRange, optional_float

FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
MAX_CHUNK_DAYS = 5
FUTURE_SKEW = timedelta(hours=6)

KNOWN_SOURCES: tuple[str, ...] = (
    "VIIRS_NOAA21_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_SNPP_NRT",
    "MODIS_NRT",
    "LANDSAT_NRT",
    "VIIRS_NOAA21_SP",
    "VIIRS_NOAA20_SP",
    "VIIRS_SNPP_SP",
    "MODIS_SP",
)

# Newest VIIRS first. NRT feeds include RT and URT rows while those rows exist.
DEFAULT_SOURCES: tuple[str, ...] = (
    "VIIRS_NOAA21_NRT",
    "VIIRS_NOAA20_NRT",
    "VIIRS_SNPP_NRT",
    "MODIS_NRT",
    "LANDSAT_NRT",
)

FIRE_COLUMNS = [
    "latitude",
    "longitude",
    "acquired_at",
    "frp",
    "confidence",
    "satellite",
    "instrument",
    "source",
    "processing_level",
    "brightness",
    "daynight",
    "version",
]


def parse_sources(text: str) -> list[str]:
    sources = [part.strip().upper() for part in text.split(",") if part.strip()]
    unknown = [source for source in sources if source not in KNOWN_SOURCES]
    if unknown:
        known = ", ".join(KNOWN_SOURCES)
        raise ValueError(f"Unknown FIRMS source(s): {', '.join(unknown)}. Known sources: {known}")
    if not sources:
        raise ValueError("at least one FIRMS source is required")
    return sources


def day_chunks(time_range: TimeRange, max_days: int = MAX_CHUNK_DAYS) -> list[tuple[date, int]]:
    """Split an inclusive date span into FIRMS day-range requests."""
    start = time_range.start.astimezone(UTC).date()
    end = time_range.end.astimezone(UTC).date()
    chunks: list[tuple[date, int]] = []
    cursor = start
    while cursor <= end:
        remaining = (end - cursor).days + 1
        days = min(max_days, remaining)
        chunks.append((cursor, days))
        cursor += timedelta(days=days)
    return chunks


def firms_url(api_key: str, source: str, bbox: BBox, day_range: int, day: date) -> str:
    coords = ",".join(f"{value:.4f}" for value in (bbox.west, bbox.south, bbox.east, bbox.north))
    return f"{FIRMS_BASE}/{api_key}/{source}/{coords}/{day_range}/{day.isoformat()}"


def firms_body_ok(text: str) -> bool:
    lowered = text.lower()
    if "rate limit" in lowered:
        return False
    if "invalid" in lowered and "key" in lowered:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return "latitude" in stripped.lower()
    return False


def processing_level(version: str) -> str:
    text = (version or "").upper()
    if "URT" in text:
        return "URT"
    if "NRT" in text:
        return "NRT"
    if re.search(r"(^|[^A-Z])RT([^A-Z]|$)", text):
        return "RT"
    return "SP"


def parse_firms_csv(text: str, source: str) -> tuple[list[dict], int]:
    """Parse an area CSV into candidate rows. The integer is a count of unreadable rows."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], 1
    rows: list[dict] = []
    unreadable = 0
    for raw in reader:
        normalized = {
            (key or "").strip().lower(): (value or "").strip()
            for key, value in raw.items()
            if key is not None
        }
        if not any(normalized.values()):
            continue
        parsed = _parse_row(normalized, source)
        if parsed is None:
            unreadable += 1
        else:
            rows.append(parsed)
    return rows, unreadable


def empty_fires() -> pd.DataFrame:
    return pd.DataFrame(columns=FIRE_COLUMNS)


def merge_detections(
    frames: list[pd.DataFrame],
    separation_km: float,
) -> tuple[pd.DataFrame, int]:
    """Keep earlier frames. Drop later rows that sit on a fire already kept.

    `frames` must already be in priority order. The integer is how many supplemental
    rows were dropped.
    """
    if not frames:
        return empty_fires(), 0
    kept_rows: list[dict] = []
    dropped = 0
    lat_step = max(separation_km, 0.1) / 111.0
    # Only earlier, higher-priority frames block a row. Pixels from one overpass
    # stay together so clustering can sum their FRP.
    buckets: dict[tuple[int, int], list[dict]] = {}
    for frame in frames:
        if frame.empty:
            continue
        accepted: list[dict] = []
        for row in frame.to_dict("records"):
            if _near_kept(row, buckets, lat_step, separation_km):
                dropped += 1
                continue
            accepted.append(row)
        for row in accepted:
            kept_rows.append(row)
            _bucket_add(buckets, row, lat_step)
    if not kept_rows:
        return empty_fires(), dropped
    return pd.DataFrame(kept_rows, columns=FIRE_COLUMNS), dropped


class FirmsClient:
    name = "firms"

    def __init__(
        self,
        api_key: str,
        http: HttpClient,
        *,
        sources: tuple[str, ...] | list[str] = DEFAULT_SOURCES,
        ttl: timedelta = timedelta(minutes=30),
        separation_km: float = 3.0,
        min_modis_confidence: float = 30.0,
        refresh: bool = False,
    ) -> None:
        self.api_key = api_key.strip()
        self.http = http
        self.sources = tuple(sources)
        self.ttl = ttl
        self.separation_km = separation_km
        self.min_modis_confidence = min_modis_confidence
        self.refresh = refresh

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        if not self.api_key:
            return SourceResult(
                data=empty_fires(),
                notices=[
                    "FIRMS_MAP_KEY is not set, so no fire detections were requested. "
                    "Register for a free key at https://firms.modaps.eosdis.nasa.gov/api/area/ "
                    "and export FIRMS_MAP_KEY."
                ],
                degraded=True,
            )
        unknown = [source for source in self.sources if source not in KNOWN_SOURCES]
        if unknown:
            raise ValueError(f"Unknown FIRMS source(s): {', '.join(unknown)}")

        notices: list[str] = []
        frames: list[pd.DataFrame] = []
        used: list[str] = []
        failed: list[str] = []
        stale = False
        for source in self.sources:
            try:
                frame, source_notices, source_stale = self._fetch_source(source, bbox, time_range)
            except HttpError as exc:
                failed.append(source)
                if _key_rejected(exc):
                    notices.append(
                        "FIRMS rejected the MAP_KEY, so the remaining sources were not queried. "
                        "Check FIRMS_MAP_KEY."
                    )
                    break
                notices.append(
                    f"{source} unavailable ({exc}). Continuing with the remaining FIRMS sources."
                )
                continue
            notices.extend(source_notices)
            stale = stale or source_stale
            used.append(source)
            frames.append(frame)

        non_empty = [frame for frame in frames if not frame.empty]
        merged, overlap = merge_detections(non_empty, self.separation_km)
        partial = any("day windows failed" in note for note in notices)
        if overlap:
            notices.append(
                f"Dropped {overlap} supplemental detections within {self.separation_km:.0f} km "
                "of a higher-priority satellite so the same fire is not counted twice."
            )
        with_rows = [source for source, frame in zip(used, frames, strict=True) if not frame.empty]
        without_rows = [source for source, frame in zip(used, frames, strict=True) if frame.empty]
        if with_rows:
            summary = f"FIRMS detections from {', '.join(with_rows)}."
            if without_rows:
                summary += f" No passing detections from {', '.join(without_rows)}."
            notices.append(summary)
        elif used and not failed:
            notices.append(
                "FIRMS returned no detections that passed quality checks in this box and window. "
                "Satellites miss fires under cloud, under canopy, and between overpasses."
            )
        if failed and not used:
            notices.append(
                "All requested FIRMS sources failed and no cached detections were available. "
                "Smoke-exposure scores are omitted."
            )
        degraded = bool(failed) or stale or (not used) or partial
        return SourceResult(
            data=merged,
            notices=notices,
            degraded=degraded,
            stale=stale,
            sources_used=used,
            sources_failed=failed,
        )

    def _fetch_source(
        self,
        source: str,
        bbox: BBox,
        time_range: TimeRange,
    ) -> tuple[pd.DataFrame, list[str], bool]:
        notices: list[str] = []
        kept_rows: list[dict] = []
        reasons: Counter[str] = Counter()
        errors: list[str] = []
        stale = False
        warned_stale = False
        chunks = day_chunks(time_range)
        for day, day_range in chunks:
            url = firms_url(self.api_key, source, bbox, day_range, day)
            try:
                payload = self.http.get_text(
                    url,
                    ttl=self.ttl,
                    refresh=self.refresh,
                    accept=firms_body_ok,
                )
            except HttpError as exc:
                errors.append(str(exc))
                if _key_rejected(exc):
                    raise
                continue
            if payload.stale and not warned_stale:
                stale = True
                warned_stale = True
                when = payload.stored_at or datetime.now(UTC)
                age = format_age(self.http.clock() - when)
                minutes = max(1, int(self.ttl.total_seconds() // 60))
                notices.append(
                    f"{source}: the live feed did not return usable data. Using cached "
                    f"detections from {age} ago, older than the {minutes} minute cache window."
                )
            parsed, unreadable = parse_firms_csv(payload.text, source)
            if unreadable:
                reasons["unreadable"] += unreadable
            for row in parsed:
                if not bbox.contains(float(row["latitude"]), float(row["longitude"])):
                    reasons["outside_bbox"] += 1
                    continue
                acquired = row["acquired_at"]
                if acquired is None:
                    reasons["invalid_time"] += 1
                    continue
                if acquired < time_range.start or acquired > time_range.end + FUTURE_SKEW:
                    reasons["outside_window"] += 1
                    continue
                kept_rows.append(row)
        if errors and not kept_rows and not reasons:
            raise HttpError(errors[0])
        filtered, qc_reasons = filter_detections(
            kept_rows,
            min_modis_confidence=self.min_modis_confidence,
        )
        reasons.update(qc_reasons)
        if errors:
            notices.append(
                f"{source}: {len(errors)} of {len(chunks)} day windows failed ({errors[0]}). "
                "The windows that returned are still used."
            )
        if filtered or reasons:
            notices.append(
                f"{source}: kept {len(filtered)} detections, dropped {format_drop_counts(reasons)}."
            )
        frame = _frame(filtered)
        frame = _dedupe(frame)
        return frame, notices, stale


def _parse_row(row: dict[str, str], source: str) -> dict | None:
    latitude = optional_float(row.get("latitude") or row.get("lat"))
    longitude = optional_float(row.get("longitude") or row.get("lon"))
    if latitude is None or longitude is None:
        return None
    acquired = _parse_acquired(row)
    if "frp" not in row or row.get("frp") in (None, ""):
        frp: object = None
    else:
        parsed_frp = optional_float(row.get("frp"))
        frp = parsed_frp if parsed_frp is not None else "invalid"
    brightness = optional_float(row.get("bright_ti4") or row.get("brightness"))
    hotspot = optional_float(row.get("type"))
    hotspot_type = int(hotspot) if hotspot is not None and hotspot == int(hotspot) else None
    version = row.get("version", "")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "acquired_at": acquired,
        "frp": frp,
        "confidence": row.get("confidence", ""),
        "satellite": row.get("satellite", ""),
        "instrument": row.get("instrument", ""),
        "source": source,
        "processing_level": processing_level(version),
        "brightness": brightness,
        "daynight": row.get("daynight") or row.get("day_night") or "",
        "version": version,
        "hotspot_type": hotspot_type,
    }


def _parse_acquired(row: dict[str, str]) -> datetime | None:
    combined = row.get("acquire_time") or row.get("acq_datetime")
    if combined:
        text = combined.replace("Z", "+00:00")
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    date_text = row.get("acq_date")
    time_text = row.get("acq_time")
    if not date_text or time_text is None or time_text == "":
        return None
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None
    digits = "".join(character for character in time_text if character.isdigit())
    if not digits:
        return None
    digits = digits.zfill(4)
    hour = int(digits[:2])
    minute = int(digits[2:4]) if len(digits) >= 4 else 0
    if hour == 24 and minute == 0:
        day = day + timedelta(days=1)
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return empty_fires()
    cleaned = [{column: row.get(column) for column in FIRE_COLUMNS} for row in rows]
    frame = pd.DataFrame(cleaned, columns=FIRE_COLUMNS)
    frame["acquired_at"] = pd.to_datetime(frame["acquired_at"], utc=True)
    return frame


def _dedupe(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    working = frame.copy()
    working["_lat"] = working["latitude"].round(4)
    working["_lon"] = working["longitude"].round(4)
    working = working.drop_duplicates(["source", "_lat", "_lon", "acquired_at", "satellite"])
    return working.drop(columns=["_lat", "_lon"]).reset_index(drop=True)


def _key_rejected(exc: HttpError) -> bool:
    if exc.status in {401, 403}:
        return True
    text = exc.body_excerpt.lower()
    return "key" in text and ("invalid" in text or "map_key" in text)


def _bucket_add(buckets: dict[tuple[int, int], list[dict]], row: dict, lat_step: float) -> None:
    cell = _cell(row, lat_step)
    buckets.setdefault(cell, []).append(row)


def _near_kept(
    row: dict,
    buckets: dict[tuple[int, int], list[dict]],
    lat_step: float,
    separation_km: float,
) -> bool:
    latitude = float(row["latitude"])
    longitude = float(row["longitude"])
    cell = _cell(row, lat_step)
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for other in buckets.get((cell[0] + dlat, cell[1] + dlon), []):
                distance = haversine_km(
                    latitude,
                    longitude,
                    float(other["latitude"]),
                    float(other["longitude"]),
                )
                if distance <= separation_km:
                    return True
    return False


def _cell(row: dict, lat_step: float) -> tuple[int, int]:
    latitude = float(row["latitude"])
    longitude = float(row["longitude"])
    cos_lat = max(0.2, abs(math.cos(math.radians(latitude))))
    lon_step = lat_step / cos_lat
    return (math.floor(latitude / lat_step), math.floor(longitude / lon_step))
