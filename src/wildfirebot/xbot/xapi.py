"""Minimal X API v2 client on the standard library: read mentions, post a reply.

Reading uses an app-only bearer token. Posting signs with OAuth 1.0a user context.
Secrets come from the environment and are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from wildfirebot.http import MAX_RETRY_WAIT_SECONDS, USER_AGENT

API = "https://api.x.com/2"
Send = Callable[[str, str, dict[str, str], bytes | None], tuple[int, str, dict[str, str]]]


class XError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class XRateLimited(XError):
    """A 429 whose wait is longer than the retry cap. Try again at the next poll."""


def urllib_send(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, str, dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return (
                response.status,
                response.read().decode("utf-8", "replace"),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read().decode("utf-8", "replace"),
            {k.lower(): v for k, v in exc.headers.items()},
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise XError(f"network error: {getattr(exc, 'reason', exc)}") from exc


def _pct(value: str) -> str:
    return quote(value, safe="-._~")


def oauth1_header(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    extra_params: dict[str, str] | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """OAuth 1.0a (HMAC-SHA1) Authorization header. A JSON body is not signed."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    parts = urlsplit(url)
    base_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
    params = dict(parse_qsl(parts.query)) | (extra_params or {}) | oauth
    normalized = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
    base = "&".join((method.upper(), _pct(base_url), _pct(normalized)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    digest = hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


@dataclass(frozen=True)
class Credentials:
    bearer: str = ""
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_secret: str = ""

    @property
    def can_post(self) -> bool:
        return all((self.api_key, self.api_secret, self.access_token, self.access_secret))

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Credentials:
        return cls(
            bearer=env.get("X_BEARER_TOKEN", ""),
            api_key=env.get("X_API_KEY", ""),
            api_secret=env.get("X_API_SECRET", ""),
            access_token=env.get("X_ACCESS_TOKEN", ""),
            access_secret=env.get("X_ACCESS_TOKEN_SECRET", ""),
        )


class XClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        send: Send = urllib_send,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        self.credentials = credentials
        self.send = send
        self.sleep = sleep
        self.max_attempts = max_attempts

    def _request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, *, retry_5xx: bool
    ) -> dict:
        waited = 0.0
        last = ""
        for attempt in range(self.max_attempts):
            status, text, response_headers = self.send(
                method, url, {"User-Agent": USER_AGENT, **headers}, body
            )
            if 200 <= status < 300:
                try:
                    return json.loads(text) if text else {}
                except ValueError as exc:
                    raise XError("X returned a body that is not JSON", status) from exc
            last = f"HTTP {status}: {text[:200]}"
            if status == 429 or (retry_5xx and status in (500, 502, 503, 504)):
                wait = _retry_after(response_headers) if status == 429 else 0.5 * 2**attempt
                if waited + wait > MAX_RETRY_WAIT_SECONDS:
                    if status == 429:
                        raise XRateLimited(f"X rate limit: {last}", status)
                    break
                if attempt + 1 < self.max_attempts:
                    self.sleep(wait)
                    waited += wait
                    continue
                if status == 429:
                    raise XRateLimited(f"X rate limit: {last}", status)
            raise XError(last, status)
        raise XError(last)

    def user_id(self, username: str) -> str:
        data = self._request(
            "GET",
            f"{API}/users/by/username/{_pct(username.lstrip('@'))}",
            self._bearer(),
            None,
            retry_5xx=True,
        )
        return str(data["data"]["id"])

    def mentions(self, user_id: str, since_id: str | None) -> tuple[list[dict], dict[str, dict]]:
        """Mentions newer than since_id (oldest first) and the referenced posts by id."""
        params = {
            "max_results": "100",
            "tweet.fields": "author_id,created_at,referenced_tweets,text,conversation_id",
            "expansions": "referenced_tweets.id",  # no author expansion: profiles cost extra
        }
        if since_id:
            params["since_id"] = since_id
        data = self._request(
            "GET",
            f"{API}/users/{_pct(user_id)}/mentions?{urlencode(params)}",
            self._bearer(),
            None,
            retry_5xx=True,
        )
        posts = sorted(data.get("data") or [], key=lambda post: int(post["id"]))
        referenced = {str(p["id"]): p for p in (data.get("includes") or {}).get("tweets", [])}
        return posts, referenced

    def reply(self, text: str, to_post_id: str) -> str:
        if not self.credentials.can_post:
            raise XError(
                "posting needs X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET"
            )
        url = f"{API}/tweets"
        c = self.credentials
        header = oauth1_header(
            "POST",
            url,
            consumer_key=c.api_key,
            consumer_secret=c.api_secret,
            token=c.access_token,
            token_secret=c.access_secret,
        )
        body = json.dumps({"text": text, "reply": {"in_reply_to_tweet_id": to_post_id}}).encode()
        data = self._request(
            "POST",
            url,
            {"Authorization": header, "Content-Type": "application/json"},
            body,
            retry_5xx=False,  # a retried 5xx could post twice
        )
        return str(data["data"]["id"])

    def _bearer(self) -> dict[str, str]:
        if not self.credentials.bearer:
            raise XError("reading mentions needs X_BEARER_TOKEN")
        return {"Authorization": f"Bearer {self.credentials.bearer}"}


def _retry_after(headers: dict[str, str]) -> float:
    try:
        if "retry-after" in headers:
            return max(0.0, float(headers["retry-after"]))
        if "x-rate-limit-reset" in headers:
            return max(0.0, float(headers["x-rate-limit-reset"]) - time.time())
    except ValueError:
        pass
    return 15 * 60.0
