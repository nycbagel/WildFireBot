"""Small HTTP helper. Stdlib only, so the demo does not need a second client library."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import quote

# (url, headers) -> (status_code, body_text). Tests pass a fake.
Transport = Callable[[str, dict[str, str]], tuple[int, str]]

DEFAULT_TIMEOUT_S = 30.0


class ClientError(Exception):
    """A data source failed in a way the outlook can report and continue past."""


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove key material, including a URL-encoded copy, from text we might print."""

    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[redacted]")
        encoded = quote(secret, safe="")
        if encoded and encoded != secret:
            redacted = redacted.replace(encoded, "[redacted]")
    return redacted


def get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    transport: Transport | None = None,
) -> tuple[int, str]:
    """GET a URL and return ``(status, body)``.

    HTTP error statuses are returned, not raised, so the caller can describe
    them without the URL (which may contain an API key). Transport failures
    raise ``ClientError`` and do not include the URL.
    """

    request_headers = dict(headers or {})
    if transport is not None:
        return transport(url, request_headers)
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), raw
    except urllib.error.URLError as exc:
        reason = exc.reason
        message = reason if isinstance(reason, str) else str(reason)
        raise ClientError(f"network error: {message}") from exc


def parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientError("response was not JSON") from exc
