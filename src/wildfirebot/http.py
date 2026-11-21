"""HTTP with retries, a local response cache, and redacted errors."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

USER_AGENT = "WildFireBot/0.1 (educational smoke-exposure forecast; +https://github.com/nycbagel/WildFireBot)"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Total time one request may spend waiting between retries. A Retry-After beyond it
# (an hourly or daily quota) cannot clear in time, so the request fails at once and the
# caller falls back to a stale cache entry or reports the feed as down.
MAX_RETRY_WAIT_SECONDS = 30.0


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class TransportError(Exception):
    """The connection failed before an HTTP status was available."""


class HttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body_excerpt: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body_excerpt = body_excerpt
        self.retry_after = retry_after


@dataclass
class HttpText:
    text: str
    from_cache: bool
    stale: bool
    stored_at: datetime | None
    url: str


class Transport(Protocol):
    def request(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        """Return a response, or raise TransportError if the connection fails."""


def redact_url(url: str) -> str:
    """Remove API keys from a URL so logs and tracebacks can be shared."""
    redacted = re.sub(
        r"(https://firms\.modaps\.eosdis\.nasa\.gov/(?:usfs/)?api/area/csv/)[^/]+/",
        r"\1<key>/",
        url,
    )
    redacted = re.sub(
        r"([?&](?:API_KEY|api_key|apikey|key)=)[^&]+",
        r"\1<key>",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def format_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours = minutes // 60
    remaining = minutes % 60
    if hours < 48:
        return f"{hours} hours {remaining} minutes"
    return f"{hours // 24} days"


class FileCache:
    """JSON cache of response bodies. The filename is a hash, so the key is not stored."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, url: str) -> tuple[str, datetime] | None:
        path = self._path(url)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            stored = datetime.fromisoformat(payload["stored_at"])
            if stored.tzinfo is None:
                stored = stored.replace(tzinfo=UTC)
            return str(payload["body"]), stored.astimezone(UTC)
        except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            return None

    def put(self, url: str, body: str, *, stored_at: datetime | None = None) -> None:
        path = self._path(url)
        when = stored_at or datetime.now(UTC)
        payload = {"stored_at": when.astimezone(UTC).isoformat(), "body": body}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)


class UrllibTransport:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def request(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        request = urllib.request.Request(url, headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                header_map = {key.lower(): value for key, value in response.headers.items()}
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            header_map = {key.lower(): value for key, value in exc.headers.items()}
            body = exc.read()
            try:
                body = decode_body(body, header_map)
            except TransportError:
                pass  # an error body only feeds the excerpt; keep the status for the retry logic
            return HttpResponse(exc.code, body, header_map)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransportError(str(reason)) from exc
        return HttpResponse(status, decode_body(body, header_map), header_map)


def decode_body(body: bytes, headers: Mapping[str, str]) -> bytes:
    """Undo Content-Encoding. urllib asks for nothing by itself, so this only runs when
    HttpClient asked for gzip or deflate."""
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding in ("", "identity"):
        return body
    try:
        if encoding in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate, no zlib header
    except (OSError, EOFError, zlib.error) as exc:
        raise TransportError(f"could not decompress a {encoding} body ({exc})") from exc
    raise TransportError(f"unsupported content encoding {encoding}")


class FixtureTransport:
    """Map live URLs onto recorded files. Used only when the CLI is pointed at a fixture dir."""

    def __init__(self, root: Path, known_firms_sources: tuple[str, ...] | list[str]) -> None:
        self.root = Path(root)
        self.known_firms_sources = tuple(known_firms_sources)

    def request(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        path = self.resolve(url)
        if path is None or not path.is_file():
            message = f"no fixture for {redact_url(url)}".encode()
            return HttpResponse(404, message, {})
        return HttpResponse(200, path.read_bytes(), {"content-type": "application/octet-stream"})

    def resolve(self, url: str) -> Path | None:
        if "firms.modaps.eosdis.nasa.gov" in url:
            for source in self.known_firms_sources:
                if f"/{source}/" in url:
                    return self.root / "firms" / f"{source}.csv"
            return None
        if "api.met.no" in url:
            return self.root / "metno.json"
        if "air-quality-api.open-meteo.com" in url:
            return self.root / "openmeteo_air_quality.json"
        if "api.open-meteo.com" in url:
            return self.root / "openmeteo.json"
        if "airnowapi.org" in url:
            if "/aq/observation/current/ziplatlong/" in url:
                return self.root / "airnow_current.json"
            return self.root / "airnow.json"
        if "api.openaq.org" in url:
            if "/latest" in url:
                match = re.search(r"/locations/(\d+)/latest", url)
                if match:
                    specific = self.root / f"openaq_latest_{match.group(1)}.json"
                    if specific.is_file():
                        return specific
                return self.root / "openaq_latest.json"
            return self.root / "openaq_locations.json"
        return None


class HttpClient:
    def __init__(
        self,
        transport: Transport,
        *,
        cache: FileCache | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.transport = transport
        self.cache = cache
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep or time.sleep
        self.clock = clock or (lambda: datetime.now(UTC))
        self.user_agent = user_agent

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        ttl: timedelta,
        refresh: bool = False,
        accept: Callable[[str], bool] | None = None,
        retry_statuses: frozenset[int] = RETRY_STATUSES,
    ) -> HttpText:
        """Return a response body, from the live feed or from cache.

        A fresh cache hit does not touch the network. If the live request fails or the
        body is unusable and an older cache entry exists, that entry is returned with
        `stale=True` so the caller can warn.
        """
        accept_fn = accept or (lambda _text: True)
        cached = self.cache.get(url) if self.cache is not None else None
        if cached is not None and not refresh and accept_fn(cached[0]):
            body, stored_at = cached
            if self.clock() - stored_at <= ttl:
                return HttpText(body, True, False, stored_at, url)

        try:
            text = self._fetch(url, headers or {}, retry_statuses)
        except HttpError:
            if cached is not None and accept_fn(cached[0]):
                return HttpText(cached[0], True, True, cached[1], url)
            raise

        if not accept_fn(text):
            excerpt = _excerpt(text.encode("utf-8"))
            error = HttpError(
                f"Unexpected response from {redact_url(url)}",
                status=200,
                body_excerpt=excerpt,
            )
            if cached is not None and accept_fn(cached[0]):
                return HttpText(cached[0], True, True, cached[1], url)
            raise error

        if self.cache is not None:
            self.cache.put(url, text, stored_at=self.clock())
        return HttpText(text, False, False, self.clock(), url)

    def _fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        retry_statuses: frozenset[int] = RETRY_STATUSES,
    ) -> str:
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/csv, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
        }
        request_headers.update(headers)
        last_error: HttpError | None = None
        attempts = 0
        waited = 0.0
        for attempt in range(self.max_attempts):
            if attempt and last_error is not None:
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                if last_error.retry_after is not None:
                    if waited + last_error.retry_after > MAX_RETRY_WAIT_SECONDS:
                        break
                    delay = max(delay, last_error.retry_after)
                if delay > 0:
                    self.sleep(delay)
                    waited += delay
            attempts += 1
            try:
                response = self.transport.request(url, request_headers)
            except TransportError as exc:
                last_error = HttpError(
                    f"Could not reach {redact_url(url)} ({exc})",
                    body_excerpt=str(exc),
                )
                continue
            if response.status in retry_statuses:
                last_error = HttpError(
                    f"HTTP {response.status} from {redact_url(url)}",
                    status=response.status,
                    body_excerpt=_excerpt(response.body),
                    retry_after=_retry_after(response.headers),
                )
                continue
            if response.status >= 400:
                raise HttpError(
                    f"HTTP {response.status} from {redact_url(url)}",
                    status=response.status,
                    body_excerpt=_excerpt(response.body),
                    retry_after=_retry_after(response.headers),
                )
            return response.body.decode("utf-8-sig", errors="replace")
        assert last_error is not None
        raise HttpError(
            f"{last_error} after {attempts} attempt{'' if attempts == 1 else 's'}",
            status=last_error.status,
            body_excerpt=last_error.body_excerpt,
            retry_after=last_error.retry_after,
        )


def _excerpt(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    return " ".join(text.split())[:180]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
