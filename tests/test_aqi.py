import pytest

from wildfirebot.model.aqi import (
    category_for_aqi,
    pm25_to_aqi,
    predict_pm25,
    round_half_up,
    truncate_pm25,
)


@pytest.mark.parametrize(
    ("concentration", "aqi", "category"),
    [
        (0.0, 0, "Good"),
        (9.0, 50, "Good"),
        (9.1, 51, "Moderate"),
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
def test_breakpoints(concentration, aqi, category):
    result = pm25_to_aqi(concentration)
    assert result.aqi == aqi
    assert result.category == category
    assert result.concentration == concentration


def test_truncates_before_converting():
    assert truncate_pm25(9.09) == 9.0
    assert truncate_pm25(9.19) == 9.1
    assert pm25_to_aqi(9.09).aqi == 50


def test_rounds_half_up_not_bankers():
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2
    assert round_half_up(68.5) == 69


def test_reported_san_francisco_reading():
    result = pm25_to_aqi(18.4)
    assert result.aqi == 68
    assert result.category == "Moderate"


def test_extends_above_500():
    result = pm25_to_aqi(400)
    assert result.aqi == 649
    assert result.category == "Hazardous"
    assert category_for_aqi(result.aqi) == "Hazardous"


def test_rejects_negative_and_missing():
    with pytest.raises(ValueError):
        pm25_to_aqi(-0.1)
    with pytest.raises(ValueError):
        pm25_to_aqi(float("nan"))


def test_model_stub_is_explicit():
    with pytest.raises(NotImplementedError, match="Milestone 2"):
        predict_pm25({"smoke_score": 1.0})
