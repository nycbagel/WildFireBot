from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.support import ScriptedTransport
from wildfirebot.geo import BBox
from wildfirebot.http import HttpClient
from wildfirebot.sources.base import TimeRange
from wildfirebot.sources.firms import (
    FirmsClient,
    day_chunks,
    firms_url,
    merge_detections,
    parse_firms_csv,
    processing_level,
)

ROOT = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 19, 21, tzinfo=UTC)
BOX = BBox(-124.0, 36.0, -120.0, 40.0)
WINDOW = TimeRange(NOW - timedelta(days=2), NOW)


def test_processing_levels_and_recorded_parsers():
    assert processing_level("2.0URT") == "URT"
    assert processing_level("6.1RT") == "RT"
    assert processing_level("6.1NRT") == "NRT"
    assert processing_level("6.1") == "SP"
    assert processing_level("1.0.7 NRT") == "NRT"

    viirs, _ = parse_firms_csv(
        (ROOT / "cli/firms/VIIRS_NOAA21_NRT.csv").read_text(), "VIIRS_NOAA21_NRT"
    )
    assert any(row["processing_level"] == "URT" for row in viirs)
    modis, _ = parse_firms_csv((ROOT / "firms_modis.csv").read_text(), "MODIS_NRT")
    assert {row["hotspot_type"] for row in modis} >= {0, 2}
    landsat, _ = parse_firms_csv((ROOT / "firms_landsat.csv").read_text(), "LANDSAT_NRT")
    assert len(landsat) == 2
    assert all(row["frp"] is None for row in landsat)
    assert landsat[0]["acquired_at"] == datetime(2026, 11, 19, 19, 5, tzinfo=UTC)


def test_url_uses_west_south_east_north_and_chunks_by_five_days():
    url = firms_url("secret", "VIIRS_NOAA20_NRT", BOX, 2, NOW.date())
    assert "/VIIRS_NOAA20_NRT/-124.0000,36.0000,-120.0000,40.0000/2/2026-11-19" in url
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 11, 7, tzinfo=UTC)
    assert day_chunks(TimeRange(start, end)) == [
        (start.date(), 5),
        (datetime(2026, 11, 6, tzinfo=UTC).date(), 2),
    ]


def test_recorded_viirs_drops_bad_rows_and_keeps_urt(tmp_path):
    body = (ROOT / "cli/firms/VIIRS_NOAA21_NRT.csv").read_text()
    transport = ScriptedTransport().when("VIIRS_NOAA21_NRT", 200, body)
    result = _client(transport, tmp_path, ("VIIRS_NOAA21_NRT",)).fetch(BOX, WINDOW)
    assert list(result.data["processing_level"]) == ["URT", "NRT", "NRT"]
    assert result.data["frp"].sum() == pytest.approx(180.5 + 40 + 90)
    assert any("low-confidence" in note for note in result.notices)
    assert result.degraded is False


def test_failed_source_falls_back_to_the_next_satellite(tmp_path):
    modis = (ROOT / "firms_modis.csv").read_text()
    transport = ScriptedTransport()
    transport.when("VIIRS_NOAA21_NRT", 503, "unavailable")
    transport.when("VIIRS_NOAA20_NRT", 200, modis)
    result = _client(
        transport,
        tmp_path,
        ("VIIRS_NOAA21_NRT", "VIIRS_NOAA20_NRT"),
    ).fetch(BOX, WINDOW)
    assert result.sources_failed == ["VIIRS_NOAA21_NRT"]
    assert "VIIRS_NOAA20_NRT" in result.sources_used
    assert result.degraded is True
    assert any("Continuing with the remaining" in note for note in result.notices)
    # Type 2 and confidence 10 are gone; the URT vegetation fire remains.
    assert set(result.data["processing_level"]) == {"NRT", "URT"}
    assert len(transport.calls) == 4  # three retries, then the fallback


def test_invalid_key_stops_the_source_list(tmp_path):
    transport = ScriptedTransport().when("VIIRS", 200, "Invalid MAP_KEY")
    result = _client(transport, tmp_path, ("VIIRS_NOAA21_NRT", "MODIS_NRT")).fetch(BOX, WINDOW)
    assert transport.calls == [] or len(transport.calls) == 1
    assert len(transport.calls) == 1
    assert "MAP_KEY" in " ".join(result.notices)
    assert "secret-key" not in " ".join(result.notices)
    assert result.data.empty


def test_missing_key_does_not_call_the_network(tmp_path):
    transport = ScriptedTransport()
    http = HttpClient(transport, backoff_seconds=0, sleep=lambda _d: None)
    result = FirmsClient("", http).fetch(BOX, WINDOW)
    assert transport.calls == []
    assert result.degraded is True
    assert "FIRMS_MAP_KEY" in result.notices[0]


def test_merge_does_not_double_count_the_same_flame():
    import pandas as pd

    when = NOW
    primary = pd.DataFrame([_fire(38.0, -122.0, 100, "VIIRS_NOAA21_NRT", when)])
    secondary = pd.DataFrame(
        [
            _fire(38.0, -122.0, 500, "VIIRS_NOAA20_NRT", when),
            _fire(39.0, -122.0, 50, "VIIRS_NOAA20_NRT", when),
        ]
    )
    merged, dropped = merge_detections([primary, secondary], separation_km=3)
    assert dropped == 1
    assert len(merged) == 2
    assert merged.iloc[0]["frp"] == 100
    assert set(merged["frp"]) == {100, 50}


def _fire(lat, lon, frp, source, when):
    return {
        "latitude": lat,
        "longitude": lon,
        "acquired_at": when,
        "frp": frp,
        "confidence": "h",
        "satellite": "N",
        "instrument": "VIIRS",
        "source": source,
        "processing_level": "NRT",
        "brightness": 300.0,
        "daynight": "D",
        "version": "2.0NRT",
    }


def _client(transport, tmp_path, sources):
    http = HttpClient(transport, max_attempts=3, backoff_seconds=0, sleep=lambda _d: None)
    return FirmsClient("secret-key", http, sources=sources, separation_km=3)
