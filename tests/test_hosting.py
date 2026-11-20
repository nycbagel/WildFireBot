"""Hosting middleware: health, bearer auth, per-IP rate limit."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wildfirebot.hosting import HostingMiddleware


def _client(**kw):
    app = Starlette(
        routes=[Route("/mcp", lambda r: PlainTextResponse("hi"), methods=["GET", "POST"])]
    )
    return TestClient(HostingMiddleware(app, **kw))


def test_open_when_no_token():
    assert _client().get("/mcp").status_code == 200


def test_health_needs_no_token():
    assert _client(token="s3cret").get("/health").json() == {"status": "ok"}


def test_token_required():
    c = _client(token="s3cret")
    assert c.get("/mcp").status_code == 401
    assert c.get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/mcp", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_rate_limit_per_ip_and_window():
    now = [0.0]
    c = _client(rate_limit=2, window_s=10, clock=lambda: now[0])
    assert c.get("/mcp").status_code == 200
    assert c.get("/mcp").status_code == 200
    r = c.get("/mcp")
    assert r.status_code == 429 and "retry-after" in r.headers
    assert c.get("/mcp", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 200
    now[0] = 11
    assert c.get("/mcp").status_code == 200
