"""Drop low-confidence and physically invalid hotspot rows before they can affect a score."""

from __future__ import annotations

from collections import Counter

from wildfirebot.sources.base import optional_float

MAX_FRP_MW = 100_000.0
DEFAULT_MIN_MODIS_CONFIDENCE = 30.0

_CATEGORICAL_OK = {"n", "nominal", "m", "medium", "h", "high"}
_CATEGORICAL_LOW = {"l", "low", "", "nan", "none", "null"}


def confidence_ok(
    value: object, min_modis_confidence: float = DEFAULT_MIN_MODIS_CONFIDENCE
) -> bool:
    """VIIRS uses n/h, Landsat uses M/H, and MODIS uses 0-100. Low classes are rejected."""
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in _CATEGORICAL_OK:
        return True
    if text in _CATEGORICAL_LOW:
        return False
    number = optional_float(text)
    if number is None:
        return False
    return number >= min_modis_confidence


def drop_reason(
    row: dict,
    *,
    min_modis_confidence: float = DEFAULT_MIN_MODIS_CONFIDENCE,
) -> str | None:
    """Return a short reason to reject `row`, or None when the row can be kept."""
    latitude = optional_float(row.get("latitude"))
    longitude = optional_float(row.get("longitude"))
    if (
        latitude is None
        or longitude is None
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return "invalid_coordinate"
    if row.get("acquired_at") is None:
        return "invalid_time"
    if not confidence_ok(row.get("confidence"), min_modis_confidence):
        return "low_confidence"
    frp = row.get("frp")
    if frp is not None:
        numeric = optional_float(frp)
        if numeric is None or numeric < 0 or numeric > MAX_FRP_MW:
            return "invalid_frp"
    hotspot_type = row.get("hotspot_type")
    if hotspot_type not in (None, 0):
        return "non_wildfire"
    return None


def filter_detections(
    rows: list[dict],
    *,
    min_modis_confidence: float = DEFAULT_MIN_MODIS_CONFIDENCE,
) -> tuple[list[dict], Counter[str]]:
    kept: list[dict] = []
    reasons: Counter[str] = Counter()
    for row in rows:
        reason = drop_reason(row, min_modis_confidence=min_modis_confidence)
        if reason is None:
            kept.append(row)
        else:
            reasons[reason] += 1
    return kept, reasons


def format_drop_counts(reasons: Counter[str]) -> str:
    if not reasons:
        return "nothing"
    labels = {
        "invalid_coordinate": "invalid coordinates",
        "invalid_time": "invalid times",
        "low_confidence": "low-confidence",
        "invalid_frp": "invalid FRP",
        "non_wildfire": "non-wildfire hotspots",
        "outside_bbox": "outside the requested box",
        "outside_window": "outside the time window",
    }
    parts = [f"{count} {labels.get(reason, reason)}" for reason, count in sorted(reasons.items())]
    return ", ".join(parts)
