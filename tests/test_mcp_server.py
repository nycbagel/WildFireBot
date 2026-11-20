"""In-process MCP client: initialize, list tools, call both tools on fixtures."""

import json
from pathlib import Path

import anyio
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from wildfirebot import DISCLAIMER
from wildfirebot.mcp import build_server

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
NOW = "2026-11-19T20:00:00+00:00"


def test_mcp_initialize_list_and_call_tools(monkeypatch):
    monkeypatch.setenv("WILDFIREBOT_FIXTURE_DIR", str(FIXTURES))
    monkeypatch.setenv("WILDFIREBOT_NOW", NOW)
    monkeypatch.setenv("WILDFIREBOT_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("FIRMS_MAP_KEY", "test-firms-key")
    monkeypatch.setenv("AIRNOW_API_KEY", "test-airnow-key")
    monkeypatch.setenv("OPENAQ_API_KEY", "test-openaq-key")

    async def _run():
        server = build_server()
        async with InMemoryTransport(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "wildfirebot"
                listed = await session.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                assert set(by_name) == {"forecast_smoke", "forecast_smoke_aqi", "pm25_to_aqi"}
                hours = by_name["forecast_smoke"].input_schema["properties"]["hours"]
                assert hours["default"] == 48
                assert "relative exposure index" in (by_name["forecast_smoke"].description or "")
                radius = by_name["forecast_smoke"].input_schema["properties"]["radius_km"]
                assert radius["default"] == 250
                assert by_name["pm25_to_aqi"].input_schema["required"] == ["concentration"]

                aqi_result = await session.call_tool("pm25_to_aqi", {"concentration": 55})
                aqi = _body(aqi_result)
                assert aqi["aqi"] == 149
                assert aqi["category"] == "Unhealthy for Sensitive Groups"
                assert aqi["disclaimer"] == DISCLAIMER

                forecast_result = await session.call_tool(
                    "forecast_smoke",
                    {
                        "lat": 37.8,
                        "lon": -122.3,
                        "hours": 6,
                        "radius_km": 200,
                        "days": 1,
                        "lookback_hours": 3,
                    },
                )
                forecast = _body(forecast_result)
                assert forecast["degraded"] is False
                assert forecast["disclaimer"] == DISCLAIMER
                assert len(forecast["clusters"]) == 2
                assert forecast["observations"][0]["aqi"] == 68
                assert forecast["peak"]["score"] > 0

                monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
                monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
                monkeypatch.delenv("OPENAQ_API_KEY", raising=False)
                missing = _body(
                    await session.call_tool(
                        "forecast_smoke",
                        {"lat": 37.8, "lon": -122.3, "hours": 6, "lookback_hours": 3, "days": 1},
                    )
                )
                assert missing["degraded"] is True
                joined = " ".join(missing["notices"])
                assert "FIRMS_MAP_KEY" in joined
                assert missing["disclaimer"] == DISCLAIMER

    anyio.run(_run)


def _body(result) -> dict:
    assert result.is_error is not True
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)
