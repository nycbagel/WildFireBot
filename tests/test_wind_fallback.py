import gzip
import json
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest

from tests.support import ScriptedTransport
from wildfirebot.aqi_forecast import build_aqi_forecast
from wildfirebot.geo import BBox, destination
from wildfirebot.http import FileCache, HttpClient, HttpResponse, TransportError, decode_body
from wildfirebot.pipeline import build_report
from wildfirebot.sources import metno
from wildfirebot.sources.base import SourceResult, TimeRange
from wildfirebot.sources.metno import MetNorwayClient, build_metno_url, parse_metno
from wildfirebot.sources.openmeteo import WIND_OMITTED, OpenMeteoClient
from wildfirebot.sources.wind_fallback import BACKUP_NOTE, FallbackWindClient
from wildfirebot.tools import build_runtime

ROOT = Path(__file__).parent / "fixtures"
METNO = (ROOT / "metno_compact.json").read_text()
OPEN_METEO = (ROOT / "cli/openmeteo.json").read_text()
NOW = datetime(2026, 11, 21, 5, 55, tzinfo=UTC)
BOX = BBox(-120.0, 37.4, -119.2, 38.1)
WINDOW = TimeRange(datetime(2026, 11, 21, 5, tzinfo=UTC), datetime(2026, 11, 21, 10, tzinfo=UTC))
LAT, LON = 37.75, -119.59


@pytest.fixture(autouse=True)
def _no_pause_carried_between_tests():
    metno.reset_pause()
    yield
    metno.reset_pause()


class HeaderTransport:
    """Answer every URL that contains `part` with one status, body and headers."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = []

    def request(self, url, headers):
        self.calls.append(url)
        self.headers.append(dict(headers))
        for part, (status, body, extra) in self.routes.items():
            if part in url:
                return HttpResponse(status, body.encode("utf-8"), extra)
        return HttpResponse(404, b"unscripted", {})


class OneFire:
    """A FIRMS stand-in: one 500 MW hotspot, seen an hour ago."""

    name = "firms"

    def __init__(self, now, bearing, distance_km):
        lat, lon = destination(LAT, LON, bearing, distance_km * 1000.0)
        self.frame = pd.DataFrame(
            [
                {
                    "latitude": lat,
                    "longitude": lon,
                    "acquired_at": pd.Timestamp(now - timedelta(hours=1)),
                    "frp": 500.0,
                    "confidence": "h",
                    "satellite": "N21",
                    "instrument": "VIIRS",
                    "source": "VIIRS_NOAA21_NRT",
                    "processing_level": "NRT",
                    "brightness": 340.0,
                    "daynight": "D",
                    "version": "2.0NRT",
                }
            ]
        )

    def fetch(self, bbox, time_range):
        return SourceResult(data=self.frame, sources_used=["VIIRS_NOAA21_NRT"])


class NoFires:
    name = "firms"

    def fetch(self, bbox, time_range):
        return SourceResult(data=pd.DataFrame(), sources_used=[])


def _http(transport, now=NOW, cache=None):
    return HttpClient(transport, cache=cache, sleep=lambda _seconds: None, clock=lambda: now)


def _wind(http, now=NOW):
    return FallbackWindClient(OpenMeteoClient(http, now=now), MetNorwayClient(http, now=now))


def _steady_open_meteo(start, hours, speed, direction):
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    n = len(times)
    return json.dumps(
        {
            "hourly_units": {"wind_speed_10m": "m/s", "wind_speed_850hPa": "m/s"},
            "hourly": {
                "time": times,
                "wind_speed_10m": [speed] * n,
                "wind_direction_10m": [direction] * n,
                "wind_speed_850hPa": [speed] * n,
                "wind_direction_850hPa": [direction] * n,
                "boundary_layer_height": [800.0] * n,
            },
        }
    )


def _steady_metno(start, hours, speed, direction):
    steps = [
        {
            "time": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": {
                "instant": {"details": {"wind_speed": speed, "wind_from_direction": direction}}
            },
        }
        for i in range(hours)
    ]
    return json.dumps(
        {"properties": {"meta": {"units": {"wind_speed": "m/s"}}, "timeseries": steps}}
    )


def test_metno_url_rounds_coordinates_to_four_decimals():
    query = {
        k: v[0]
        for k, v in parse_qs(urlparse(build_metno_url(37.7512345, -119.5987654)).query).items()
    }
    assert query == {"lat": "37.7512", "lon": "-119.5988"}


def test_parse_metno_reads_the_10m_wind_and_leaves_the_rest_missing():
    frame, notices = parse_metno(json.loads(METNO))
    assert notices == []
    assert len(frame) == 7
    first = frame.iloc[0]
    assert str(first["time"]) == "2026-11-21 05:00:00+00:00"
    assert first["wind_speed_10m"] == pytest.approx(3.1)
    assert first["wind_direction_10m"] == pytest.approx(76.6)
    for column in ("wind_speed_850hPa", "wind_direction_850hPa", "boundary_layer_height"):
        assert frame[column].isna().all()


def test_parse_metno_refuses_an_unknown_wind_unit():
    payload = json.loads(METNO)
    payload["properties"]["meta"]["units"]["wind_speed"] = "furlongs/fortnight"
    with pytest.raises(ValueError):
        parse_metno(payload)


def test_open_meteo_answer_is_used_and_met_norway_is_not_asked():
    transport = ScriptedTransport().when("api.open-meteo.com", 200, OPEN_METEO)
    transport.when("api.met.no", 200, METNO)
    now = datetime(2026, 11, 19, 20, tzinfo=UTC)
    result = _wind(_http(transport, now), now).fetch(
        BOX, TimeRange(now - timedelta(hours=12), now + timedelta(hours=1))
    )
    assert result.sources_used == ["open-meteo"]
    assert not result.data.empty
    assert not any("api.met.no" in url for url in transport.calls)


def test_rate_limited_open_meteo_falls_back_to_met_norway():
    transport = ScriptedTransport().when(
        "api.open-meteo.com", 429, "Daily API request limit exceeded"
    )
    transport.when("api.met.no", 200, METNO)
    result = _wind(_http(transport)).fetch(BOX, WINDOW)
    assert result.sources_used == ["met-norway"]
    assert result.sources_failed == ["open-meteo"]
    assert result.degraded is True
    # the window is 05:00 to 10:00: the six hourly steps of the fixture, not the later one
    assert list(result.data["wind_speed_10m"].round(1)) == [3.1, 2.1, 2.2, 2.9, 2.9, 2.4]
    text = " ".join(result.notices)
    assert "HTTP 429" in text
    assert BACKUP_NOTE in result.notices
    assert "CC BY 4.0" in BACKUP_NOTE and "creativecommons.org/licenses/by/4.0" in BACKUP_NOTE
    assert WIND_OMITTED not in text
    assert sum("api.open-meteo.com" in url for url in transport.calls) == 3


def test_both_wind_feeds_down_keeps_both_reasons_and_says_scores_are_omitted():
    transport = ScriptedTransport().when("api.open-meteo.com", 429, "limit")
    transport.when("api.met.no", 503, "down")
    result = _wind(_http(transport)).fetch(BOX, WINDOW)
    assert result.data.empty
    assert result.degraded is True
    assert result.sources_used == []
    assert result.sources_failed == ["open-meteo", "met-norway"]
    text = " ".join(result.notices)
    assert "Open-Meteo wind forecast is unavailable" in text
    assert "MET Norway wind forecast is unavailable" in text
    assert text.count(WIND_OMITTED) == 1


def test_backup_holds_its_first_wind_back_over_the_lookback():
    transport = ScriptedTransport().when("api.met.no", 200, METNO)
    window = TimeRange(WINDOW.start - timedelta(hours=11), WINDOW.end)
    result = MetNorwayClient(_http(transport), now=NOW).fetch(BOX, window)
    times = pd.to_datetime(result.data["time"], utc=True)
    assert times.iloc[0] == pd.Timestamp("2026-11-20 18:00", tz="UTC")
    assert len(result.data) == 11 + 6
    held = result.data.iloc[:11]
    assert held["wind_speed_10m"].tolist() == pytest.approx([3.1] * 11)
    assert held["wind_direction_10m"].tolist() == pytest.approx([76.6] * 11)
    assert held["boundary_layer_height"].isna().all()
    assert any("11 hour(s)" in note and "05:00 UTC" in note for note in result.notices)


def test_hourly_score_with_the_backup_matches_open_meteo_under_steady_wind():
    now = datetime(2026, 11, 21, 6, 23, tzinfo=UTC)
    firms = OneFire(now, bearing=270.0, distance_km=150.0)
    open_meteo = _steady_open_meteo(datetime(2026, 11, 20, tzinfo=UTC), 96, 5.0, 270.0)
    met = _steady_metno(datetime(2026, 11, 21, 6, tzinfo=UTC), 60, 5.0, 270.0)

    def report(open_meteo_status):
        transport = ScriptedTransport().when("api.open-meteo.com", open_meteo_status, open_meteo)
        transport.when("api.met.no", 200, met)
        return build_report(
            latitude=LAT,
            longitude=LON,
            hours=12,
            radius_km=250,
            detection_days=2,
            cluster_km=3,
            lookback_hours=12,
            now=now,
            firms=firms,
            weather=_wind(_http(transport, now), now),
            airnow=None,
            openaq=None,
            data_mode="test",
        )

    answered, refused = report(200), report(429)
    assert answered.hourly[0].score > 1.0  # the fire is upwind and close enough to matter
    for hour in (0, 1, 6, 11):
        assert refused.hourly[hour].score == pytest.approx(answered.hourly[hour].score)
        assert not any("wind gap" in note for note in refused.hourly[hour].notes)
    assert any("assume that hour's wind held steady" in note for note in refused.notices)


def test_aqi_horizon_after_the_hourly_range_uses_the_six_hourly_step():
    transport = ScriptedTransport().when("api.open-meteo.com", 429, "limit")
    transport.when("api.met.no", 200, METNO)
    report = build_aqi_forecast(
        latitude=LAT,
        longitude=LON,
        radius_km=150,
        detection_days=1,
        cluster_km=3,
        now=NOW,
        firms=NoFires(),
        weather=_wind(_http(transport)),
        air_quality=None,
        airnow=None,
        openaq=None,
        data_mode="test",
        horizons=[0, 59],
        use_terrain=False,
    )
    # +59 h is 16:00 on the 23rd; the backup's next step is 18:00, two hours later
    assert report.horizons[-1].wind_speed_m_s == pytest.approx(0.9)
    assert report.horizons[0].wind_speed_m_s == pytest.approx(3.1)


def test_metno_429_is_not_retried_and_pauses_later_requests():
    transport = HeaderTransport({"api.met.no": (429, "slow down", {"retry-after": "1800"})})
    first = MetNorwayClient(_http(transport)).fetch(BOX, WINDOW)
    assert first.data.empty
    assert "HTTP 429" in " ".join(first.notices)
    assert len(transport.calls) == 1
    assert metno.paused_until() == NOW + timedelta(minutes=30)

    second = MetNorwayClient(_http(transport, NOW + timedelta(minutes=5))).fetch(BOX, WINDOW)
    assert second.data.empty
    assert "not asked again until 06:25 UTC" in " ".join(second.notices)
    assert len(transport.calls) == 1


def test_metno_pause_without_retry_after_lasts_ten_minutes():
    transport = HeaderTransport({"api.met.no": (429, "slow down", {})})
    MetNorwayClient(_http(transport)).fetch(BOX, WINDOW)
    assert metno.paused_until() == NOW + timedelta(minutes=10)
    later = MetNorwayClient(_http(transport, NOW + timedelta(minutes=11))).fetch(BOX, WINDOW)
    assert len(transport.calls) == 2
    assert later.data.empty


def test_metno_pause_still_answers_from_the_cache(tmp_path):
    cache = FileCache(tmp_path)
    url = build_metno_url(*BOX.centroid)
    cache.put(url, METNO, stored_at=NOW - timedelta(hours=2))
    metno._pause(NOW + timedelta(minutes=10))
    transport = HeaderTransport({})
    result = MetNorwayClient(_http(transport, cache=cache)).fetch(BOX, WINDOW)
    assert transport.calls == []
    assert result.sources_used == ["met-norway"]
    assert result.stale is True
    assert (
        "MET Norway did not answer. Using its forecast from 2 hours 0 minutes ago."
        in result.notices
    )


def test_live_runtime_uses_open_meteo_with_the_met_norway_backup(tmp_path):
    runtime = build_runtime(cache_dir=tmp_path, refresh=True)
    assert isinstance(runtime.weather, FallbackWindClient)
    assert isinstance(runtime.weather.primary, OpenMeteoClient)
    assert isinstance(runtime.weather.backup, MetNorwayClient)
    assert runtime.weather.primary.refresh is True
    assert not hasattr(runtime.weather.backup, "refresh")


def test_requests_accept_gzip_and_bodies_are_decoded():
    transport = HeaderTransport({"api.met.no": (200, METNO, {})})
    MetNorwayClient(_http(transport)).fetch(BOX, WINDOW)
    assert "gzip" in transport.headers[0]["Accept-Encoding"]

    body = b'{"ok": true}'
    assert decode_body(gzip.compress(body), {"content-encoding": "gzip"}) == body
    assert decode_body(zlib.compress(body), {"content-encoding": "deflate"}) == body
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    assert decode_body(raw.compress(body) + raw.flush(), {"content-encoding": "deflate"}) == body
    assert decode_body(body, {}) == body
    with pytest.raises(TransportError):
        decode_body(b"not gzip", {"content-encoding": "gzip"})
    with pytest.raises(TransportError):
        decode_body(body, {"content-encoding": "br"})
