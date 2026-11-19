import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.support import ScriptedTransport

from wildfirebot.geo import BBox
from wildfirebot.http import HttpClient
from wildfirebot.sources.airnow import AirNowClient, parse_airnow
from wildfirebot.sources.base import TimeRange
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient, build_openmeteo_url, parse_openmeteo

ROOT = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 19, 20, tzinfo=UTC)
BOX = BBox(-123.0, 37.2, -121.8, 38.4)
WINDOW = TimeRange(NOW - timedelta(days=1), NOW + timedelta(hours=1))


def test_openmeteo_recorded_fixture_keeps_the_gap_and_requests_both_levels():
    payload = json.loads((ROOT / "cli/openmeteo.json").read_text())
    frame, notices = parse_openmeteo(payload)
    assert notices == []
    nulls = frame["wind_speed_10m"].isna()
    assert int(nulls.sum()) == 1
    assert str(frame.loc[nulls, "time"].iloc[0]).startswith("2026-11-19 22:00")

    url = build_openmeteo_url(37.8, -122.3, WINDOW, now=NOW)
    assert "wind_speed_10m" in url
    assert "wind_speed_850hPa" in url
    assert "wind_direction_850hPa" in url
    assert "boundary_layer_height" in url
    assert "wind_speed_unit=ms" in url
    assert "latitude=37.8000" in url

    transport = ScriptedTransport().when("open-meteo", 200, json.dumps(payload))
    client = OpenMeteoClient(_http(transport), now=NOW)
    result = client.fetch(BOX, WINDOW)
    assert result.sources_used == ["open-meteo"]
    assert not result.data.empty


def test_openmeteo_converts_kilometers_per_hour():
    payload = {
        "hourly_units": {"wind_speed_10m": "km/h", "wind_speed_850hPa": "km/h"},
        "hourly": {
            "time": ["2026-11-19T20:00"],
            "wind_speed_10m": [36.0],
            "wind_direction_10m": [90],
            "wind_speed_850hPa": [72.0],
            "wind_direction_850hPa": [180],
            "boundary_layer_height": [500],
        },
    }
    frame, _notes = parse_openmeteo(payload)
    assert frame.iloc[0]["wind_speed_10m"] == pytest.approx(10.0)
    assert frame.iloc[0]["wind_speed_850hPa"] == pytest.approx(20.0)


def test_airnow_hourly_and_current_fallback(tmp_path):
    hourly = (ROOT / "cli/airnow.json").read_text()
    current = (ROOT / "airnow_current.json").read_text()
    parsed = parse_airnow(json.loads(current))
    assert list(parsed["site_name"]) == ["San Francisco"]
    assert parsed.iloc[0]["pm25"] is None or parsed.iloc[0]["pm25"] != parsed.iloc[0]["pm25"]
    assert parsed.iloc[0]["reported_aqi"] == 68

    transport = ScriptedTransport()
    transport.when("/aq/data/", 503, "down")
    transport.when("latLong/current", 200, current)
    result = AirNowClient("air-key", _http(transport)).fetch(BOX, WINDOW)
    assert result.degraded is True
    assert result.sources_used == ["airnow"]
    assert "hourly box feed failed" in " ".join(result.notices)
    assert len(result.data) == 1
    assert "air-key" not in " ".join(result.notices)

    ok = ScriptedTransport().when("/aq/data/", 200, hourly)
    live = AirNowClient("air-key", _http(ok)).fetch(BOX, WINDOW)
    assert list(live.data["site_name"]) == ["Oakland West", "San Francisco"]
    assert live.data.iloc[0]["pm25"] == pytest.approx(18.4)


def test_openaq_joins_location_to_latest_pm25():
    locations = (ROOT / "cli/openaq_locations.json").read_text()
    latest = (ROOT / "cli/openaq_latest_1001.json").read_text()
    transport = ScriptedTransport()
    transport.when("/locations?", 200, locations)
    transport.when("/latest", 200, latest)
    result = OpenAQClient("oq-key", _http(transport)).fetch(BOX, WINDOW)
    assert len(result.data) == 1
    row = result.data.iloc[0]
    assert row["site_name"] == "Berkeley Aquatic Park"
    assert row["pm25"] == pytest.approx(12.0)
    assert row["source"] == "openaq"
    assert "oq-key" not in " ".join(transport.calls)


def test_missing_observation_keys_are_degraded_and_offline():
    transport = ScriptedTransport()
    air = AirNowClient("", _http(transport)).fetch(BOX, WINDOW)
    oq = OpenAQClient("", _http(transport)).fetch(BOX, WINDOW)
    assert transport.calls == []
    assert air.degraded and oq.degraded
    assert "AIRNOW_API_KEY" in air.notices[0]
    assert "OPENAQ_API_KEY" in oq.notices[0]


def _http(transport):
    return HttpClient(transport, max_attempts=2, backoff_seconds=0, sleep=lambda _d: None)
