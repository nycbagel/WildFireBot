"""MCP server for WildFireBot.

stdio is the default so `wildfirebot-mcp` works as a local agent tool.
`--http` (or setting PORT) serves the same tools over streamable HTTP with optional\nbearer-token auth and a per-IP rate limit; see wildfirebot.hosting.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from wildfirebot import DISCLAIMER, __version__
from wildfirebot.tools import TOOLS, call_tool, runtime_from_env

FORECAST_DESCRIPTION = (
    "Estimate relative wildfire smoke exposure at one latitude and longitude from "
    "NASA FIRMS fire detections and an Open-Meteo wind forecast. "
    "Returns nearby fire clusters, a wind summary, an hourly smoke-exposure score, "
    "nearby PM2.5 readings when monitors respond, degraded-mode notices, and a disclaimer. "
    "The score is a relative exposure index, not an official AQI and not a health advisory. "
    "Reads FIRMS_MAP_KEY, AIRNOW_API_KEY, and OPENAQ_API_KEY from the environment. "
    "A missing key is named in notices; the server keeps running."
)

AQI_FORECAST_DESCRIPTION = (
    "Forecast PM2.5 and the US EPA AQI at one latitude and longitude. "
    "Adds a Gaussian plume from each NASA FIRMS fire cluster, using fire radiative "
    "power and the Open-Meteo wind forecast, on top of Open-Meteo's background PM2.5. "
    "Works for a new fire because it does not need a history of observations. "
    "Returns horizons, degraded-mode notices, and a disclaimer. "
    "This is a screening estimate, not an official AQI and not a health advisory. "
    "Missing FIRMS_MAP_KEY, AIRNOW_API_KEY, or OPENAQ_API_KEY is reported in notices."
)

PM25_DESCRIPTION = (
    "Convert one PM2.5 concentration in micrograms per cubic meter to the US EPA 2024 "
    "AQI with the breakpoint equation. Does not compute a 24-hour average or a NowCast. "
    "The result includes the AQI, the category, and a disclaimer that this is not a health advisory."
)


def build_server() -> MCPServer:
    """Register the forecast tools on a new server instance."""
    server = MCPServer(
        name="wildfirebot",
        title="WildFireBot",
        version=__version__,
        instructions=(
            "WildFireBot estimates relative smoke exposure from satellite fire detections "
            "and forecast wind. The score is not an official AQI and not a health advisory. "
            + DISCLAIMER
        ),
    )

    @server.tool(name="forecast_smoke", description=FORECAST_DESCRIPTION)
    def forecast_smoke(
        lat: Annotated[float, Field(description="Target latitude in decimal degrees (-90 to 90)")],
        lon: Annotated[
            float, Field(description="Target longitude in decimal degrees (-180 to 180)")
        ],
        hours: Annotated[int, Field(description="Forecast length in hours", ge=1, le=120)] = 48,
        radius_km: Annotated[
            float, Field(description="Fire search radius in kilometers", gt=0, le=2000)
        ] = 250,
        days: Annotated[
            int, Field(description="Days of FIRMS detections to read", ge=1, le=10)
        ] = 2,
        lookback_hours: Annotated[
            int, Field(description="Hours to walk the back-trajectory", ge=1, le=48)
        ] = 12,
    ) -> dict[str, Any]:
        """Relative smoke exposure for one location. Not an official AQI."""
        try:
            return call_tool(
                "forecast_smoke",
                {
                    "lat": lat,
                    "lon": lon,
                    "hours": hours,
                    "radius_km": radius_km,
                    "days": days,
                    "lookback_hours": lookback_hours,
                },
                runtime_from_env(),
            )
        except ValueError as exc:
            return {
                "degraded": True,
                "notices": [str(exc)],
                "disclaimer": DISCLAIMER,
                "latitude": lat,
                "longitude": lon,
            }

    @server.tool(name="forecast_smoke_aqi", description=AQI_FORECAST_DESCRIPTION)
    def forecast_smoke_aqi(
        lat: Annotated[float, Field(description="Target latitude in decimal degrees (-90 to 90)")],
        lon: Annotated[
            float, Field(description="Target longitude in decimal degrees (-180 to 180)")
        ],
        radius_km: Annotated[
            float, Field(description="Fire search radius in kilometers", gt=0, le=2000)
        ] = 150,
        days: Annotated[
            int, Field(description="Days of FIRMS detections to read", ge=1, le=10)
        ] = 1,
        stability: Annotated[
            str, Field(description="Pasquill-Gifford stability class A through F")
        ] = "D",
        plume_height_m: Annotated[
            float, Field(description="Assumed plume height in meters", gt=0)
        ] = 200,
        emission_factor: Annotated[
            float, Field(description="PM2.5 emission in grams per second per MW of FRP", gt=0)
        ] = 5.5,
    ) -> dict[str, Any]:
        """Gaussian plume PM2.5 and AQI. Not a health advisory."""
        try:
            return call_tool(
                "forecast_smoke_aqi",
                {
                    "lat": lat,
                    "lon": lon,
                    "radius_km": radius_km,
                    "days": days,
                    "stability": stability,
                    "plume_height_m": plume_height_m,
                    "emission_factor": emission_factor,
                },
                runtime_from_env(),
            )
        except ValueError as exc:
            return {
                "degraded": True,
                "notices": [str(exc)],
                "disclaimer": DISCLAIMER,
                "latitude": lat,
                "longitude": lon,
            }

    @server.tool(name="pm25_to_aqi", description=PM25_DESCRIPTION)
    def pm25_to_aqi(
        concentration: Annotated[
            float,
            Field(
                description="PM2.5 concentration in micrograms per cubic meter",
                ge=0,
            ),
        ],
    ) -> dict[str, Any]:
        """EPA 2024 PM2.5 breakpoint AQI. Not a health advisory."""
        try:
            result = call_tool("pm25_to_aqi", {"concentration": concentration})
        except ValueError as exc:
            return {"degraded": True, "notices": [str(exc)], "disclaimer": DISCLAIMER}
        result["disclaimer"] = DISCLAIMER
        result["note"] = (
            "Breakpoint AQI of this concentration using the EPA 2024 PM2.5 scale. "
            "Not a 24-hour average and not a health advisory."
        )
        return result

    return server


def list_tools() -> list[dict]:
    """Static tool catalog. The live server advertises the same names."""
    return TOOLS


def main(argv: list[str] | None = None) -> None:
    """Serve tools over stdio, or over streamable HTTP when --http is set."""
    parser = argparse.ArgumentParser(
        prog="wildfirebot-mcp",
        description=(
            "WildFireBot MCP server. Stdio by default. "
            "The smoke score is a relative exposure index, not an official AQI."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve streamable HTTP instead of stdio (default path /mcp)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP bind address (default 127.0.0.1, or 0.0.0.0 when PORT is set)",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="HTTP port (default $PORT, else 8000)"
    )
    args = parser.parse_args(argv)
    _load_local_env()
    server = build_server()
    hosted = "PORT" in os.environ
    if args.http or hosted:
        from wildfirebot.hosting import serve

        port = args.port or int(os.environ.get("PORT", "8000"))
        host = args.host or ("0.0.0.0" if hosted else "127.0.0.1")
        serve(server, host, port)
        return
    server.run("stdio")


def _load_local_env() -> None:
    """Fill missing keys from .env (working directory, then the repo checkout). Existing env wins."""
    candidates = [Path(".env"), Path(__file__).resolve().parents[2] / ".env"]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
