"""Client parsing against checked-in fixtures. The transport never touches the network."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wildfirebot.clients.airnow import fetch_airnow
from wildfirebot.clients.firms import fetch_hotspots, normalize_acq_time
from wildfirebot.clients.nws import (
    fetch_hourly_winds,
    parse_wind_from_deg,
    parse_wind_speed_m_s,
)
from wildfirebot.clients.open_meteo import fetch_pm25_forecast

FIXTURES = Path(__file__).parent / "fixtures"
USER_AGENT = "WildFireBot (contact@example.com)"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class Router:
    def __init__(self, routes: dict[str, tuple[int, str]]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        self.calls.append((url, headers or {}))
        for prefix, response in self.routes.items():
            if prefix in url:
                return response
        raise AssertionError(f"unexpected URL {url}")


def test_acq_time_is_zero_padded_utc():
    assert normalize_acq_time("5") == "0005"
    assert normalize_acq_time("1830") == "1830"
    assert normalize_acq_time("1.0") == "0001"


def test_firms_csv_returns_the_requested_fields_and_skips_a_bad_frp():
    body = _text("firms_viirs.csv")
    router = Router({"firms.modaps.eosdis.nasa.gov": (200, body)})
    result = fetch_hotspots(
        37.9,
        -122.3,
        50,
        map_key="MAP-SECRET",
        source="VIIRS_NOAA20_NRT",
        user_agent=USER_AGENT,
        transport=router,
    )
    assert result.status == "ok"
    assert len(result.hotspots) == 2
    first = result.hotspots[0]
    assert first.lat == pytest.approx(37.95)
    assert first.lon == pytest.approx(-122.4)
    assert first.frp == pytest.approx(12.5)
    assert first.confidence == "n"
    assert first.acq_date == "2026-11-19"
    assert first.acq_time == "1830"
    assert result.hotspots[1].confidence == "h"
    assert result.hotspots[1].acq_time == "0005"
    url = router.calls[0][0]
    assert url.startswith(
        "https://firms.modaps.eosdis.nasa.gov/api/area/csv/MAP-SECRET/VIIRS_NOAA20_NRT/"
    )
    assert url.endswith("/1")
    assert "MAP-SECRET" not in result.detail


def test_modis_confidence_stays_numeric_text():
    router = Router({"firms.modaps.eosdis.nasa.gov": (200, _text("firms_modis.csv"))})
    result = fetch_hotspots(
        37.5,
        -121.9,
        40,
        map_key="MAP-SECRET",
        source="MODIS_NRT",
        user_agent=USER_AGENT,
        transport=router,
    )
    assert result.status == "ok"
    assert result.hotspots[0].confidence == "85"
    assert result.hotspots[0].frp == pytest.approx(22.0)
    assert "/MODIS_NRT/" in router.calls[0][0]


@pytest.mark.parametrize("source", ["VIIRS_NOAA21_NRT", "VIIRS_SNPP_NRT"])
def test_other_allowed_viirs_sources_are_placed_in_the_url(source):
    router = Router({"firms.modaps.eosdis.nasa.gov": (200, _text("firms_viirs.csv"))})
    fetch_hotspots(
        37.9,
        -122.3,
        20,
        map_key="MAP-SECRET",
        source=source,
        user_agent=USER_AGENT,
        transport=router,
    )
    assert f"/{source}/" in router.calls[0][0]


def test_unknown_firms_source_is_rejected_without_a_request():
    def boom(url, headers=None):
        raise AssertionError("should not request FIRMS")

    with pytest.raises(ValueError):
        fetch_hotspots(
            0, 0, 10, map_key="MAP-SECRET", source="LANDSAT_NRT", user_agent=USER_AGENT, transport=boom
        )


def test_missing_firms_key_skips_without_a_request():
    def boom(url, headers=None):
        raise AssertionError("should not request FIRMS")

    result = fetch_hotspots(
        0, 0, 10, map_key="", source="VIIRS_NOAA20_NRT", user_agent=USER_AGENT, transport=boom
    )
    assert result.status == "skipped"
    assert "FIRMS_MAP_KEY" in result.detail


def test_firms_error_body_does_not_leak_the_key():
    router = Router({"firms.modaps.eosdis.nasa.gov": (200, "Invalid key MAP-SECRET")})
    result = fetch_hotspots(
        37.0,
        -122.0,
        25,
        map_key="MAP-SECRET",
        source="VIIRS_NOAA20_NRT",
        user_agent=USER_AGENT,
        transport=router,
    )
    assert result.status == "error"
    assert "MAP-SECRET" not in result.detail
    assert "[redacted]" in result.detail


def test_wind_parsers():
    assert parse_wind_from_deg("W") == 270
    assert parse_wind_from_deg("sw") == 225
    assert parse_wind_from_deg("North") == 0
    assert parse_wind_speed_m_s("10 mph") == pytest.approx(10 * 0.44704)
    assert parse_wind_speed_m_s("5 to 15 mph") == pytest.approx(10 * 0.44704)
    assert parse_wind_speed_m_s("Calm") == 0.0
    with pytest.raises(ValueError):
        parse_wind_from_deg("sideways")


def test_nws_follows_the_hourly_link_and_sends_a_user_agent():
    router = Router(
        {
            "api.weather.gov/points": (200, _text("nws_points.json")),
            "api.weather.gov/gridpoints": (200, _text("nws_hourly.json")),
        }
    )
    result = fetch_hourly_winds(37.87, -122.27, user_agent=USER_AGENT, transport=router)
    assert result.status == "ok"
    assert [url for url, _headers in router.calls] == [
        "https://api.weather.gov/points/37.87,-122.27",
        "https://api.weather.gov/gridpoints/MTR/85,105/forecast/hourly",
    ]
    assert router.calls[0][1]["User-Agent"] == USER_AGENT
    assert router.calls[1][1]["User-Agent"] == USER_AGENT
    covering = result.at(datetime(2026, 11, 19, 12, 50, tzinfo=timezone.utc))
    assert covering is not None
    assert covering.direction_text == "W"
    assert covering.speed_m_s == pytest.approx(10 * 0.44704)
    # 13:50 is inside the 13:00 hour even though 14:00 is the nearer start.
    later = result.at(datetime(2026, 11, 19, 13, 50, tzinfo=timezone.utc))
    assert later is not None
    assert later.direction_text == "SW"
    assert later.speed_m_s == pytest.approx(10 * 0.44704)


def test_airnow_uses_the_new_routes_and_inverts_a_nowcast_aqi():
    def transport(url, headers=None):
        calls.append(url)
        if "observation/current/ziplatlong" in url:
            return 200, _text("airnow_current.json")
        if "forecast/current" in url:
            return 200, _text("airnow_forecast.json")
        raise AssertionError(url)

    calls: list[str] = []
    result = fetch_airnow(37.87, -122.27, api_key="AIRNOW-SECRET", user_agent=USER_AGENT, transport=transport)
    assert result.status == "ok"
    assert result.reporting_area == "Oakland"
    assert result.aqi == 42
    assert result.pm25_from == "inverted_aqi"
    assert result.pm25_ug_m3 == pytest.approx(42 / 50 * 9.0)
    assert [item.parameter for item in result.forecasts] == ["PM2.5", "O3"]
    assert result.forecasts[0].aqi == 55
    observation_url = calls[0]
    forecast_url = calls[1]
    assert observation_url.startswith("https://www.airnowapi.org/aq/observation/current/ziplatlong/")
    assert "format=application/json" in observation_url
    assert "distance=25" in observation_url
    assert "API_KEY=AIRNOW-SECRET" in observation_url
    assert "latitude=37.87" in observation_url
    assert "longitude=-122.27" in observation_url
    assert "/aq/observation/latLong/current/" not in observation_url
    assert forecast_url.startswith("https://www.airnowapi.org/aq/forecast/current/")
    assert "AIRNOW-SECRET" not in result.detail


def test_airnow_prefers_an_explicit_concentration():
    def transport(url, headers=None):
        if "observation" in url:
            return 200, _text("airnow_current_concentration.json")
        return 200, "[]"

    result = fetch_airnow(37.87, -122.27, api_key="AIRNOW-SECRET", user_agent=USER_AGENT, transport=transport)
    assert result.pm25_from == "concentration"
    assert result.pm25_ug_m3 == pytest.approx(8.4)
    assert result.category == "Good"


def test_airnow_webservice_error_is_a_short_message_without_the_key():
    # Live AirNow returns HTTP 401 with this wrapper when the key is rejected.
    body = '{"WebServiceError":[{"Message":"Invalid API key AIRNOW-SECRET"}]}'

    def transport(url, headers=None):
        return 401, body

    result = fetch_airnow(
        37.87, -122.27, api_key="AIRNOW-SECRET", user_agent=USER_AGENT, transport=transport
    )
    assert result.status == "error"
    assert "Invalid API key" in result.detail
    assert "AIRNOW-SECRET" not in result.detail
    assert "[redacted]" in result.detail


def test_missing_airnow_key_skips_both_routes():
    def boom(url, headers=None):
        raise AssertionError("should not request AirNow")

    result = fetch_airnow(37.0, -122.0, api_key="", user_agent=USER_AGENT, transport=boom)
    assert result.status == "skipped"
    assert "AIRNOW_API_KEY" in result.detail


def test_open_meteo_hourly_pm25_needs_no_key_and_floors_to_the_hour():
    router = Router({"air-quality-api.open-meteo.com": (200, _text("open_meteo.json"))})
    result = fetch_pm25_forecast(37.87, -122.27, user_agent=USER_AGENT, transport=router)
    assert result.status == "ok"
    url = router.calls[0][0]
    assert url.startswith("https://air-quality-api.open-meteo.com/v1/air-quality?")
    assert "hourly=pm2_5" in url
    assert "API_KEY" not in url
    noon = datetime(2026, 11, 19, 12, 40, tzinfo=timezone.utc)
    assert result.at(noon) == pytest.approx(5.0)
    evening = datetime(2026, 11, 19, 18, 10, tzinfo=timezone.utc)
    assert result.at(evening) == pytest.approx(6.5)
    # The null 13:00 hour is dropped, so 13:30 still sees the noon value.
    assert result.at(datetime(2026, 11, 19, 13, 30, tzinfo=timezone.utc)) == pytest.approx(5.0)


def test_open_meteo_fixture_is_valid_json():
    payload = json.loads(_text("open_meteo.json"))
    assert payload["hourly"]["pm2_5"][0] == 5.0
