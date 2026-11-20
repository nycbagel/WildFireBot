"""ASGI wrapper for hosting the MCP server: health check, bearer token, per-IP rate limit.

Configuration (environment):
  PORT                  HTTP port (default 8000)
  WFB_ACCESS_TOKEN      if set, every /mcp request needs `Authorization: Bearer <token>`
  WFB_RATE_LIMIT        requests per window per client IP (default 60, 0 disables)
  WFB_RATE_WINDOW_S     window length in seconds (default 60)
"""

from __future__ import annotations

import hmac
import json
import os
import time
from collections import defaultdict, deque


class HostingMiddleware:
    """Pure ASGI middleware so streaming responses are never buffered."""

    def __init__(
        self,
        app,
        token: str | None = None,
        rate_limit: int = 60,
        window_s: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        self.app = app
        self.token = token or None
        self.rate_limit = rate_limit
        self.window_s = window_s
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in ("/health", "/healthz"):
            await _json(send, 200, {"status": "ok"})
            return
        if self.token and not self._authorized(scope):
            await _json(
                send,
                401,
                {"error": "missing or invalid bearer token"},
                [(b"www-authenticate", b"Bearer")],
            )
            return
        retry = self._limited(self._client_ip(scope))
        if retry:
            await _json(
                send,
                429,
                {"error": "rate limit exceeded", "retry_after_s": retry},
                [(b"retry-after", str(retry).encode())],
            )
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for name, value in scope["headers"]:
            if name == b"authorization":
                scheme, _, supplied = value.decode("latin-1").partition(" ")
                return scheme.lower() == "bearer" and hmac.compare_digest(
                    supplied.strip().encode(), self.token.encode()
                )
        return False

    @staticmethod
    def _client_ip(scope) -> str:
        # Render, Fly, and cloudflared all set X-Forwarded-For; the first entry is the client.
        for name, value in scope["headers"]:
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _limited(self, ip: str) -> int:
        """Return seconds to wait if over the limit, else 0 and record the hit."""
        if self.rate_limit <= 0:
            return 0
        now = self.clock()
        hits = self._hits[ip]
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if len(hits) >= self.rate_limit:
            return max(1, int(self.window_s - (now - hits[0])) + 1)
        hits.append(now)
        if len(self._hits) > 10000:  # drop idle clients
            for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]:
                del self._hits[key]
        return 0


async def _json(send, status: int, body: dict, extra: list | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    await send(
        {"type": "http.response.start", "status": status, "headers": headers + (extra or [])}
    )
    await send({"type": "http.response.body", "body": payload})


def build_app(server):
    """Streamable-HTTP MCP app (stateless) wrapped with health, auth, and rate limiting."""
    inner = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0"
    )
    return HostingMiddleware(
        inner,
        token=os.environ.get("WFB_ACCESS_TOKEN"),
        rate_limit=int(os.environ.get("WFB_RATE_LIMIT", "60")),
        window_s=float(os.environ.get("WFB_RATE_WINDOW_S", "60")),
    )


def serve(server, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(build_app(server), host=host, port=port, proxy_headers=False, log_level="info")
