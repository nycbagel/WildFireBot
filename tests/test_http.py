from datetime import UTC, datetime, timedelta

import pytest

from tests.support import ScriptedTransport
from wildfirebot.http import (
    FileCache,
    HttpClient,
    HttpError,
    HttpResponse,
    TransportError,
    redact_url,
)

NOW = datetime(2026, 11, 19, 20, tzinfo=UTC)


class DownTransport:
    def __init__(self):
        self.calls = []

    def request(self, url, headers):
        del headers
        self.calls.append(url)
        raise TransportError("connection reset")


def _client(transport, tmp_path, attempts=3):
    return HttpClient(
        transport,
        cache=FileCache(tmp_path),
        max_attempts=attempts,
        backoff_seconds=0,
        sleep=lambda _delay: None,
        clock=lambda: NOW,
    )


def test_retries_then_succeeds():
    transport = ScriptedTransport().sequence(
        "open-meteo",
        [(503, "busy"), (503, "busy"), (200, "ok-body")],
    )
    client = _client(transport, _path())
    text = client.get_text("https://api.open-meteo.com/v1/forecast", ttl=timedelta(minutes=5))
    assert text.text == "ok-body"
    assert text.stale is False
    assert len(transport.calls) == 3


def test_does_not_retry_client_errors():
    transport = ScriptedTransport().when("airnow", 400, "bad request")
    client = _client(transport, _path())
    with pytest.raises(HttpError) as caught:
        client.get_text(
            "https://www.airnowapi.org/aq/data/?API_KEY=sekret", ttl=timedelta(minutes=5)
        )
    assert caught.value.status == 400
    assert len(transport.calls) == 1
    assert "sekret" not in str(caught.value)


def test_redacts_firms_key_and_serves_stale_cache(tmp_path):
    url = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/supersecret/VIIRS_NOAA21_NRT/1,2,3,4/1/2026-11-19"
    cache = FileCache(tmp_path)
    cache.put(url, "latitude\n", stored_at=NOW - timedelta(hours=6))
    transport = DownTransport()
    client = HttpClient(
        transport,
        cache=cache,
        max_attempts=3,
        backoff_seconds=0.25,
        sleep=lambda _delay: None,
        clock=lambda: NOW,
    )
    slept = []
    client.sleep = slept.append
    text = client.get_text(url, ttl=timedelta(minutes=30), accept=lambda body: "latitude" in body)
    assert text.stale is True
    assert text.from_cache is True
    assert text.text.startswith("latitude")
    assert len(transport.calls) == 3
    assert slept == [0.25, 0.5]
    assert "supersecret" not in redact_url(url)


def test_fresh_cache_skips_the_network(tmp_path):
    url = "https://api.open-meteo.com/v1/forecast?q=1"
    cache = FileCache(tmp_path)
    cache.put(url, "cached-wind", stored_at=NOW - timedelta(minutes=5))
    transport = DownTransport()
    client = HttpClient(
        transport, cache=cache, clock=lambda: NOW, backoff_seconds=0, sleep=lambda _d: None
    )
    text = client.get_text(url, ttl=timedelta(minutes=30))
    assert text.text == "cached-wind"
    assert text.stale is False
    assert text.from_cache is True
    assert transport.calls == []


def test_unusable_body_falls_back_to_cache(tmp_path):
    url = "https://api.open-meteo.com/v1/forecast?q=2"
    cache = FileCache(tmp_path)
    cache.put(url, '{"hourly": {"time": []}}', stored_at=NOW - timedelta(hours=3))
    transport = ScriptedTransport().when("open-meteo", 200, "not json")
    client = HttpClient(
        transport, cache=cache, clock=lambda: NOW, backoff_seconds=0, sleep=lambda _d: None
    )
    text = client.get_text(url, ttl=timedelta(minutes=30), accept=lambda body: body.startswith("{"))
    assert text.stale is True
    assert "hourly" in text.text


def _path():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


class QuotaTransport:
    """Answer HTTP 429 with a Retry-After header, the way a spent quota does."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        self.calls = []

    def request(self, url, headers):
        del headers
        self.calls.append(url)
        return HttpResponse(429, b"limit exceeded", {"retry-after": str(self.retry_after)})


def _quota_client(transport, tmp_path, slept):
    return HttpClient(
        transport,
        cache=FileCache(tmp_path),
        max_attempts=3,
        backoff_seconds=0.5,
        sleep=slept.append,
        clock=lambda: NOW,
    )


def test_a_quota_retry_after_fails_at_once_and_serves_the_stale_cache(tmp_path):
    url = "https://api.open-meteo.com/v1/forecast?latitude=37.7500"
    transport, slept = QuotaTransport(3600), []
    client = _quota_client(transport, tmp_path, slept)
    client.cache.put(url, "cached-body", stored_at=NOW - timedelta(hours=2))
    text = client.get_text(url, ttl=timedelta(minutes=60))
    assert text.stale is True
    assert text.text == "cached-body"
    assert len(transport.calls) == 1
    assert slept == []


def test_a_quota_retry_after_without_a_cache_reports_one_attempt(tmp_path):
    transport, slept = QuotaTransport(3600), []
    client = _quota_client(transport, tmp_path, slept)
    with pytest.raises(HttpError) as caught:
        client.get_text("https://api.open-meteo.com/v1/forecast", ttl=timedelta(minutes=60))
    assert caught.value.status == 429
    assert caught.value.retry_after == 3600
    assert str(caught.value).endswith("after 1 attempt")
    assert slept == []


def test_a_short_retry_after_is_honoured_within_the_wait_budget(tmp_path):
    transport, slept = QuotaTransport(2), []
    client = _quota_client(transport, tmp_path, slept)
    with pytest.raises(HttpError) as caught:
        client.get_text("https://api.open-meteo.com/v1/forecast", ttl=timedelta(minutes=60))
    assert slept == [2.0, 2.0]
    assert len(transport.calls) == 3
    assert str(caught.value).endswith("after 3 attempts")
