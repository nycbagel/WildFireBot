"""HTTP clients for FIRMS, NWS, AirNow, and Open-Meteo."""

from wildfirebot.clients.airnow import fetch_airnow
from wildfirebot.clients.firms import fetch_hotspots
from wildfirebot.clients.nws import fetch_hourly_winds
from wildfirebot.clients.open_meteo import fetch_pm25_forecast

__all__ = [
    "fetch_airnow",
    "fetch_hotspots",
    "fetch_hourly_winds",
    "fetch_pm25_forecast",
]
