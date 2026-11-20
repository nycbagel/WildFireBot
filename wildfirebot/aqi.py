"""Convert PM2.5 to the US EPA Air Quality Index using the 2024 breakpoints.

The breakpoints are the PM2.5 rows of 40 CFR Part 58, Appendix G, after the
February 2024 revision (effective for AQI reporting on 6 May 2024). The 2012
table put the top of Good at 12.0 µg/m³. The 2024 table puts it at 9.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

# (c_lo, c_hi, i_lo, i_hi, category) with concentrations in µg/m³.
# The last concentration, 325.4, is the value EPA assigns to AQI 500.
PM25_BREAKPOINTS: tuple[tuple[Decimal, Decimal, int, int, str], ...] = (
    (Decimal("0.0"), Decimal("9.0"), 0, 50, "Good"),
    (Decimal("9.1"), Decimal("35.4"), 51, 100, "Moderate"),
    (Decimal("35.5"), Decimal("55.4"), 101, 150, "Unhealthy for Sensitive Groups"),
    (Decimal("55.5"), Decimal("125.4"), 151, 200, "Unhealthy"),
    (Decimal("125.5"), Decimal("225.4"), 201, 300, "Very Unhealthy"),
    (Decimal("225.5"), Decimal("325.4"), 301, 500, "Hazardous"),
)

HEALTH_MESSAGES = {
    "Good": (
        "Air quality is satisfactory, and air pollution poses little or no risk."
    ),
    "Moderate": (
        "Air quality is acceptable. Unusually sensitive people should consider "
        "shorter or less intense outdoor activity."
    ),
    "Unhealthy for Sensitive Groups": (
        "People with heart or lung disease, older adults, children, and pregnant "
        "people should reduce prolonged or intense outdoor activity."
    ),
    "Unhealthy": (
        "Everyone should reduce prolonged or intense outdoor activity. Sensitive "
        "groups should avoid it."
    ),
    "Very Unhealthy": (
        "Everyone should avoid prolonged or intense outdoor activity. Sensitive "
        "groups should stay indoors when they can."
    ),
    "Hazardous": (
        "Everyone should avoid outdoor physical activity and keep indoor activity low."
    ),
}


@dataclass(frozen=True)
class AqiResult:
    aqi: int
    category: str
    health_message: str
    concentration_ug_m3: float

    def as_dict(self) -> dict[str, object]:
        return {
            "aqi": self.aqi,
            "category": self.category,
            "health_message": self.health_message,
            "concentration_ug_m3": self.concentration_ug_m3,
        }


def truncate_pm25(concentration_ug_m3: float) -> Decimal:
    """Truncate PM2.5 to one decimal, matching EPA's AQI convention."""

    return Decimal(str(concentration_ug_m3)).quantize(Decimal("0.1"), rounding=ROUND_DOWN)


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _index(concentration: Decimal, c_lo: Decimal, c_hi: Decimal, i_lo: int, i_hi: int) -> int:
    # I = (I_hi - I_lo) / (C_hi - C_lo) * (C - C_lo) + I_lo
    span_c = c_hi - c_lo
    span_i = Decimal(i_hi - i_lo)
    index = span_i / span_c * (concentration - c_lo) + Decimal(i_lo)
    return _round_half_up(index)


def aqi_from_pm25(concentration_ug_m3: float) -> AqiResult:
    """Return the AQI category for a PM2.5 concentration in µg/m³.

    Above 325.4 µg/m³ the Hazardous category is kept and the same slope is
    extended, so the index can exceed 500. That slope is the one EPA defines
    between AQI 301 and AQI 500.
    """

    if concentration_ug_m3 < 0:
        raise ValueError("PM2.5 concentration cannot be negative")
    concentration = truncate_pm25(concentration_ug_m3)
    for c_lo, c_hi, i_lo, i_hi, category in PM25_BREAKPOINTS:
        if c_lo <= concentration <= c_hi:
            return AqiResult(
                aqi=_index(concentration, c_lo, c_hi, i_lo, i_hi),
                category=category,
                health_message=HEALTH_MESSAGES[category],
                concentration_ug_m3=float(concentration),
            )
    c_lo, c_hi, i_lo, i_hi, category = PM25_BREAKPOINTS[-1]
    return AqiResult(
        aqi=_index(concentration, c_lo, c_hi, i_lo, i_hi),
        category=category,
        health_message=HEALTH_MESSAGES[category],
        concentration_ug_m3=float(concentration),
    )


def pm25_from_aqi(aqi: float) -> float:
    """Invert the breakpoint equation to a PM2.5 concentration in µg/m³.

    AirNow's current-observation payload often carries a NowCast AQI and no
    concentration. Inverting the same table recovers the concentration that
    displays as that AQI. A NowCast is not a 24-hour average, so this is only
    a stand-in for the missing micrograms.
    """

    if aqi < 0:
        raise ValueError("AQI cannot be negative")
    value = Decimal(str(aqi))
    for c_lo, c_hi, i_lo, i_hi, _category in PM25_BREAKPOINTS:
        if Decimal(i_lo) <= value <= Decimal(i_hi):
            span_c = c_hi - c_lo
            span_i = Decimal(i_hi - i_lo)
            concentration = span_c / span_i * (value - Decimal(i_lo)) + c_lo
            return float(concentration)
    c_lo, _c_hi, i_lo, i_hi, _category = PM25_BREAKPOINTS[-1]
    span_c = PM25_BREAKPOINTS[-1][1] - c_lo
    span_i = Decimal(i_hi - i_lo)
    concentration = span_c / span_i * (value - Decimal(i_lo)) + c_lo
    return float(concentration)
