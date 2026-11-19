"""EPA PM2.5 breakpoint conversion, using the 2024 revised scale.

The breakpoints are the 24-hour PM2.5 values in 40 CFR Appendix G to Part 58
(revised 2024). Concentrations are truncated to one decimal place before the
piecewise linear conversion, and the index is rounded half up to an integer.
Values above 325.4 µg/m³ keep the Hazardous category and extend the last segment,
so the index can pass 500.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

# (concentration low, concentration high, AQI low, AQI high, category)
PM25_BREAKPOINTS: tuple[tuple[float, float, int, int, str], ...] = (
    (0.0, 9.0, 0, 50, "Good"),
    (9.1, 35.4, 51, 100, "Moderate"),
    (35.5, 55.4, 101, 150, "Unhealthy for Sensitive Groups"),
    (55.5, 125.4, 151, 200, "Unhealthy"),
    (125.5, 225.4, 201, 300, "Very Unhealthy"),
    (225.5, 325.4, 301, 500, "Hazardous"),
)


@dataclass(frozen=True)
class AQIResult:
    aqi: int
    category: str
    concentration: float


def truncate_pm25(concentration: float) -> float:
    """Truncate a non-negative concentration to one decimal, the EPA PM2.5 convention."""
    scaled = Decimal(str(concentration)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    truncated = scaled.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    return float(truncated)


def round_half_up(value: float) -> int:
    if math.isnan(value) or math.isinf(value):
        raise ValueError("AQI value is not finite")
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def category_for_aqi(aqi: int) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for Sensitive Groups"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


def pm25_to_aqi(concentration: float) -> AQIResult:
    """Convert a PM2.5 concentration in µg/m³ to an AQI and category.

    This is the breakpoint equation only. It does not compute a 24-hour average
    or a NowCast. Callers that pass an hourly reading should say so.
    """
    if concentration is None or isinstance(concentration, bool):
        raise ValueError("PM2.5 concentration is missing")
    try:
        numeric = float(concentration)
    except (TypeError, ValueError) as exc:
        raise ValueError("PM2.5 concentration is not a number") from exc
    if math.isnan(numeric) or math.isinf(numeric):
        raise ValueError("PM2.5 concentration is missing")
    if numeric < 0:
        raise ValueError("PM2.5 concentration cannot be negative")

    truncated = truncate_pm25(numeric)
    chosen = _segment_for(truncated)
    c_lo, c_hi, i_lo, i_hi, category = chosen
    if c_hi == c_lo:
        index = float(i_lo)
    else:
        index = (i_hi - i_lo) / (c_hi - c_lo) * (truncated - c_lo) + i_lo
    if truncated > PM25_BREAKPOINTS[-1][1]:
        category = "Hazardous"
    return AQIResult(aqi=round_half_up(index), category=category, concentration=truncated)


def _segment_for(concentration: float) -> tuple[float, float, int, int, str]:
    for segment in PM25_BREAKPOINTS:
        if segment[0] <= concentration <= segment[1]:
            return segment
    last = PM25_BREAKPOINTS[-1]
    if concentration > last[1]:
        return last
    for segment in PM25_BREAKPOINTS:
        if concentration <= segment[1]:
            return segment
    return last


def predict_pm25(features: object) -> float:
    """Milestone 2 stub. Milestone 1 does not estimate a concentration."""
    del features
    raise NotImplementedError(
        "Milestone 2 will train a PM2.5 model on the smoke-exposure score and monitor "
        "observations. Milestone 1 does not estimate a concentration."
    )
