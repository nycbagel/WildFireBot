import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support import ScriptedTransport
from wildfirebot.cli import main
from wildfirebot.fires.cluster import Cluster
from wildfirebot.geo import BBox, destination
from wildfirebot.http import HttpClient
from wildfirebot.model.aqi import pm25_to_aqi
from wildfirebot.model.plume import (
    PlumeConfig,
    angle_difference,
    arrival_hours,
    fire_pm25_at,
    plume_pm25,
    sigmas,
    travel_direction,
)
from wildfirebot.sources.airnow import parse_airnow
from wildfirebot.sources.base import TimeRange
from wildfirebot.sources.openmeteo_aq import (
    OpenMeteoAirQualityClient,
    build_air_quality_url,
    parse_air_quality,
)
from wildfirebot.tools import build_runtime, call_tool

ROOT = Path(__file__).parent / "fixtures"
FIXTURES = ROOT / "cli"
NOW = "2026-11-19T20:00:00+00:00"
WHEN = datetime(2026, 11, 19, 20, tzinfo=UTC)
TARGET = (37.8, -122.3)


def _cluster(latitude, longitude, frp):
    return Cluster(
        latitude=latitude,
        longitude=longitude,
        frp_mw=frp,
        count=1,
        sources=("VIIRS_NOAA20_NRT",),
        processing_levels=("NRT",),
        first_seen=WHEN,
        last_seen=WHEN,
        satellites=("N20",),
    )


def _north_of_target(km, frp=100.0):
    latitude, longitude = destination(TARGET[0], TARGET[1], 0.0, km * 1000.0)
    return _cluster(latitude, longitude, frp)


def test_sigmas_grow_with_distance_and_stability_matters():
    near_y, near_z = sigmas(1_000.0, "D")
    far_y, far_z = sigmas(10_000.0, "D")
    assert far_y > near_y and far_z > near_z
    assert sigmas(5_000.0, "A")[1] > sigmas(5_000.0, "F")[1]


def test_directions_and_arrival():
    assert travel_direction(0.0) == 180.0
    assert travel_direction(270.0) == 90.0
    assert angle_difference(350.0, 10.0) == pytest.approx(20.0)
    assert arrival_hours(36.0, 10.0) == pytest.approx(1.0)
    assert arrival_hours(3.6, 0.0) == pytest.approx(1.0)


def test_plume_scales_with_frp_wind_and_emission_factor():
    base = plume_pm25(100.0, 20.0, 0.0, 5.0)
    assert base > 0
    assert plume_pm25(200.0, 20.0, 0.0, 5.0) == pytest.approx(2 * base)
    assert plume_pm25(100.0, 20.0, 0.0, 10.0) == pytest.approx(base / 2)
    doubled = PlumeConfig(emission_factor_g_s_per_mw=11.0)
    assert plume_pm25(100.0, 20.0, 0.0, 5.0, doubled) == pytest.approx(2 * base)
    assert plume_pm25(100.0, 40.0, 0.0, 5.0) < base
    assert plume_pm25(100.0, 20.0, 20.0, 5.0) < base


def test_plume_edge_cases():
    assert plume_pm25(0.0, 10.0, 0.0, 5.0) == 0.0
    assert plume_pm25(-5.0, 10.0, 0.0, 5.0) == 0.0
    assert plume_pm25(100.0, 10.0, 95.0, 5.0) == 0.0
    assert plume_pm25(5_000.0, 10.0, 0.0, 5.0) == pytest.approx(plume_pm25(3_000.0, 10.0, 0.0, 5.0))
    assert plume_pm25(100.0, 0.0, 0.0, 5.0) == pytest.approx(plume_pm25(100.0, 1.0, 0.0, 5.0))
    with pytest.raises(ValueError, match="stability"):
        PlumeConfig(stability="Z")


def test_fire_pm25_waits_for_smoke_to_arrive():
    cluster = _north_of_target(36.0)
    # Wind from the north at 10 m/s: 36 km takes one hour.
    now_total, parts = fire_pm25_at([cluster], *TARGET, 0.0, 10.0, 0.0)
    later_total, _ = fire_pm25_at([cluster], *TARGET, 0.0, 10.0, 6.0)
    assert now_total == 0.0
    assert parts[0].downwind is True
    assert parts[0].arrival_hours == pytest.approx(1.0, abs=0.01)
    assert later_total > 0


def test_upwind_fire_adds_nothing_and_total_is_capped():
    cluster = _north_of_target(20.0, frp=500.0)
    upwind, parts = fire_pm25_at([cluster], *TARGET, 180.0, 5.0, 48.0)
    assert upwind == 0.0
    assert parts[0].downwind is False
    huge = [_north_of_target(2.0, frp=3_000.0) for _ in range(20)]
    capped, _ = fire_pm25_at(huge, *TARGET, 0.0, 1.0, 48.0)
    assert capped == pytest.approx(500.0)


def test_open_meteo_air_quality_parse_and_failure():
    payload = json.loads((FIXTURES / "openmeteo_air_quality.json").read_text())
    frame = parse_air_quality(payload)
    assert len(frame) == len(payload["hourly"]["time"]) - 1
    window = TimeRange(WHEN, WHEN + timedelta(hours=48))
    url = build_air_quality_url(*TARGET, window, now=WHEN)
    assert "pm2_5" in url and "forecast_days=3" in url

    box = BBox(-123.0, 37.2, -121.8, 38.4)
    down = ScriptedTransport().when("air-quality", 503, "down")
    http = HttpClient(down, max_attempts=1, backoff_seconds=0, sleep=lambda _d: None)
    result = OpenMeteoAirQualityClient(http, now=WHEN).fetch(box, window)
    assert result.degraded is True
    assert result.data.empty
    assert "unavailable" in result.notices[0]


def test_airnow_ziplatlong_camel_case_rows_take_the_request_point():
    payload = json.loads((ROOT / "airnow_ziplatlong.json").read_text())
    assert parse_airnow(payload).empty
    frame = parse_airnow(payload, default_location=(37.8, -122.3))
    assert list(frame["site_name"]) == ["Oakland West"]
    row = frame.iloc[0]
    assert row["reported_aqi"] == 8
    assert row["latitude"] == pytest.approx(37.8)
    assert row["time_label"] == "2026-11-19 15:00 PST"


def test_forecast_smoke_aqi_tool_on_fixtures(monkeypatch):
    _keys(monkeypatch)
    runtime = build_runtime(fixture_dir=FIXTURES, now=WHEN)
    result = call_tool(
        "forecast_smoke_aqi",
        # Directly south of the fixture's main cluster, with wind from the north.
        {"lat": 37.8, "lon": -122.4, "radius_km": 200, "horizons": [0, 6], "now": NOW},
        runtime,
    )
    assert result["data_mode"].startswith("fixtures")
    assert result["degraded"] is False
    assert result["fire_clusters"] == 2
    now_row, later_row = result["horizons"]
    assert now_row["fire_pm25"] == 0.0
    assert later_row["fire_pm25"] > 0
    assert later_row["contributing_clusters"] >= 1
    assert now_row["baseline_source"] == "open-meteo forecast"
    for row in result["horizons"]:
        assert row["aqi"] == pm25_to_aqi(row["pm25"]).aqi
    assert result["worst"]["horizon_hours"] == 6
    assert result["clusters"][0]["downwind_now"] is True
    assert result["model"]["emission_factor_g_s_per_mw"] == 5.5


def test_forecast_smoke_aqi_flags_horizons_without_wind(monkeypatch):
    _keys(monkeypatch)
    runtime = build_runtime(fixture_dir=FIXTURES, now=WHEN)
    result = call_tool("forecast_smoke_aqi", {"lat": 37.8, "lon": -122.3, "now": NOW}, runtime)
    assert [row["horizon_hours"] for row in result["horizons"]] == [0, 6, 12, 24, 48]
    assert result["degraded"] is True
    assert any("no forecast wind" in note for note in result["notices"])
    assert result["horizons"][-1]["fire_pm25"] == 0.0
    with pytest.raises(ValueError, match="latitude"):
        call_tool("forecast_smoke_aqi", {"lat": 95, "lon": 0}, runtime)


def test_cli_aqi_subcommand(tmp_path, monkeypatch, capsys):
    _keys(monkeypatch)
    args = [
        "aqi",
        "--lat",
        "37.8",
        "--lon",
        "-122.4",
        "--radius-km",
        "200",
        "--horizons",
        "0,6",
        "--now",
        NOW,
        "--fixture-dir",
        str(FIXTURES),
        "--env-file",
        str(tmp_path / "missing.env"),
    ]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "Gaussian plume" in output
    assert "DATA MODE: fixtures (not live)" in output
    assert "smoke arrives in about" in output
    assert "not a health advisory" in output

    assert main([*args, "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["horizons"][1]["fire_pm25"] > 0


def _keys(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "test-firms-key")
    monkeypatch.setenv("AIRNOW_API_KEY", "test-airnow-key")
    monkeypatch.setenv("OPENAQ_API_KEY", "test-openaq-key")
    monkeypatch.setenv("WILDFIREBOT_BACKOFF_SECONDS", "0")
