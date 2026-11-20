"""EPA 2024 PM2.5 breakpoints. These lock the table that replaced the 2012 cutoffs."""

import pytest
from decimal import Decimal

from wildfirebot.aqi import aqi_from_pm25, pm25_from_aqi, truncate_pm25
from wildfirebot.aqi import _round_half_up


@pytest.mark.parametrize(
    ("concentration", "aqi", "category"),
    [
        (0.0, 0, "Good"),
        (9.0, 50, "Good"),
        (9.1, 51, "Moderate"),
        (12.0, 56, "Moderate"),
        (35.4, 100, "Moderate"),
        (35.5, 101, "Unhealthy for Sensitive Groups"),
        (55.4, 150, "Unhealthy for Sensitive Groups"),
        (55.5, 151, "Unhealthy"),
        (125.4, 200, "Unhealthy"),
        (125.5, 201, "Very Unhealthy"),
        (225.4, 300, "Very Unhealthy"),
        (225.5, 301, "Hazardous"),
        (325.4, 500, "Hazardous"),
    ],
)
def test_2024_breakpoints(concentration, aqi, category):
    result = aqi_from_pm25(concentration)
    assert result.aqi == aqi
    assert result.category == category
    assert result.health_message


def test_good_category_ends_at_9_not_the_2012_value_of_12():
    """12.0 µg/m³ was AQI 50 before 2024. It is Moderate now."""

    assert aqi_from_pm25(9.0).category == "Good"
    assert aqi_from_pm25(12.0).category == "Moderate"
    assert aqi_from_pm25(12.0).aqi == 56


def test_concentration_is_truncated_to_one_decimal_before_the_index():
    assert truncate_pm25(9.09) == Decimal("9.0")
    assert aqi_from_pm25(9.09).aqi == 50
    assert aqi_from_pm25(9.09).category == "Good"
    assert truncate_pm25(35.49) == Decimal("35.4")
    assert aqi_from_pm25(35.49).aqi == 100
    assert aqi_from_pm25(35.5).aqi == 101


def test_half_up_rounding_does_not_use_bankers_rounding():
    assert _round_half_up(Decimal("10.5")) == 11
    assert _round_half_up(Decimal("11.5")) == 12


def test_hazardous_slope_continues_past_aqi_500():
    result = aqi_from_pm25(425.4)
    assert result.category == "Hazardous"
    expected = _round_half_up(
        Decimal(301) + (Decimal("425.4") - Decimal("225.5")) * Decimal(199) / Decimal("99.9")
    )
    assert result.aqi == expected
    assert result.aqi > 500


def test_negative_concentration_is_rejected():
    with pytest.raises(ValueError):
        aqi_from_pm25(-0.1)


@pytest.mark.parametrize(
    ("aqi", "concentration"),
    [
        (0, 0.0),
        (50, 9.0),
        (51, 9.1),
        (100, 35.4),
        (101, 35.5),
        (150, 55.4),
        (151, 55.5),
        (200, 125.4),
        (201, 125.5),
        (300, 225.4),
        (301, 225.5),
        (500, 325.4),
    ],
)
def test_aqi_inverts_through_the_same_breakpoints(aqi, concentration):
    assert pm25_from_aqi(aqi) == pytest.approx(concentration, abs=1e-9)


def test_inverting_a_midrange_aqi_round_trips():
    concentration = pm25_from_aqi(80)
    assert aqi_from_pm25(concentration).aqi == 80
    assert aqi_from_pm25(concentration).category == "Moderate"
