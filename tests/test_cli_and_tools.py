import inspect
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wildfirebot.cli import main
from wildfirebot.http import UrllibTransport
from wildfirebot.mcp import list_tools
from wildfirebot.sources.airnow import AirNowClient
from wildfirebot.sources.firms import FirmsClient
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient
from wildfirebot.tools import build_runtime, call_tool

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
NOW = "2026-11-19T20:00:00+00:00"


def test_sources_share_one_fetch_signature():
    for cls in (FirmsClient, OpenMeteoClient, AirNowClient, OpenAQClient):
        params = inspect.signature(cls.fetch).parameters
        assert "bbox" in params
        assert "time_range" in params
        assert cls.name


def test_runtime_defaults_to_live_transport(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    runtime = build_runtime(cache_dir=tmp_path)
    assert runtime.data_mode == "live"
    assert isinstance(runtime.http.transport, UrllibTransport)


def test_cli_live_default_reports_a_down_feed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WILDFIREBOT_BACKOFF_SECONDS", "0")
    for name in ("FIRMS_MAP_KEY", "AIRNOW_API_KEY", "OPENAQ_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def explode(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("url"))
        raise urllib.error.URLError("no network in this test")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    code = main(
        [
            "forecast",
            "--lat",
            "37.8",
            "--lon",
            "-122.3",
            "--hours",
            "2",
            "--cache-dir",
            str(tmp_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert calls, "the default CLI should attempt a live request"
    assert "DATA MODE: live" in output
    assert "DEGRADED MODE" in output
    assert "FIRMS_MAP_KEY" in output
    assert "fixtures" not in output.split("DATA MODE: live", 1)[0]


def test_cli_fixture_forecast_end_to_end(tmp_path, monkeypatch, capsys):
    _keys(monkeypatch)
    code = main(
        [
            "forecast",
            "--lat",
            "37.8",
            "--lon",
            "-122.3",
            "--hours",
            "6",
            "--lookback-hours",
            "3",
            "--radius-km",
            "200",
            "--days",
            "1",
            "--now",
            NOW,
            "--fixture-dir",
            str(FIXTURES),
            "--cache-dir",
            str(tmp_path),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "DATA MODE: fixtures (not live)" in output
    assert "DEGRADED MODE" not in output
    assert "Fire clusters (2)" in output
    assert "URT" in output
    assert "Wind summary" in output
    assert "interpolated" in output
    assert "breakpoint AQI 68" in output
    assert "Moderate" in output
    assert "Oakland West" in output
    assert "not a health advisory" in output


def test_cli_names_the_firms_source_that_failed(tmp_path, monkeypatch, capsys):
    _keys(monkeypatch)
    broken = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, broken)
    (broken / "firms" / "VIIRS_NOAA21_NRT.csv").unlink()
    noaa20 = broken / "firms" / "VIIRS_NOAA20_NRT.csv"
    noaa20.write_text(
        "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
        "38.2000,-122.4000,330,0.4,0.4,2026-11-19,1800,N20,VIIRS,h,2.0NRT,290,120,D\n",
        encoding="utf-8",
    )
    code = main(
        [
            "forecast",
            "--lat",
            "37.8",
            "--lon",
            "-122.3",
            "--hours",
            "3",
            "--lookback-hours",
            "2",
            "--radius-km",
            "200",
            "--days",
            "1",
            "--now",
            NOW,
            "--fixture-dir",
            str(broken),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "DEGRADED MODE" in output
    assert "VIIRS_NOAA21_NRT" in output
    assert "Continuing with the remaining FIRMS sources" in output
    assert "VIIRS_NOAA20_NRT" in output


def test_mcp_tools_return_json(monkeypatch):
    _keys(monkeypatch)
    assert [tool["name"] for tool in list_tools()] == ["forecast_smoke", "pm25_to_aqi"]
    converted = call_tool("pm25_to_aqi", {"concentration": 18.4})
    assert converted == {"aqi": 68, "category": "Moderate", "concentration": 18.4}

    runtime = build_runtime(fixture_dir=FIXTURES, now=_now())
    forecast = call_tool(
        "forecast_smoke",
        {
            "lat": 37.8,
            "lon": -122.3,
            "hours": 6,
            "lookback_hours": 3,
            "radius_km": 200,
            "days": 1,
            "now": NOW,
        },
        runtime,
    )
    assert forecast["degraded"] is False
    assert forecast["data_mode"].startswith("fixtures")
    assert len(forecast["clusters"]) == 2
    assert forecast["observations"][0]["aqi"] == 68
    assert forecast["peak"]["score"] > 0
    assert any("interpolated" in note for hour in forecast["hourly"] for note in hour["notes"])
    with pytest.raises(ValueError, match="Unknown tool"):
        call_tool("not-a-tool", {})


def _keys(monkeypatch):
    monkeypatch.setenv("FIRMS_MAP_KEY", "test-firms-key")
    monkeypatch.setenv("AIRNOW_API_KEY", "test-airnow-key")
    monkeypatch.setenv("OPENAQ_API_KEY", "test-openaq-key")
    monkeypatch.setenv("WILDFIREBOT_BACKOFF_SECONDS", "0")


def _now():
    from datetime import datetime

    return datetime.fromisoformat(NOW)
