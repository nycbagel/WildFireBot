"""Data clients. Each one implements fetch(bbox, time_range) -> SourceResult."""

from wildfirebot.sources.airnow import AirNowClient
from wildfirebot.sources.base import BBox, DataSource, SourceResult, TimeRange
from wildfirebot.sources.firms import DEFAULT_SOURCES, FirmsClient
from wildfirebot.sources.metno import MetNorwayClient
from wildfirebot.sources.openaq import OpenAQClient
from wildfirebot.sources.openmeteo import OpenMeteoClient
from wildfirebot.sources.openmeteo_aq import OpenMeteoAirQualityClient
from wildfirebot.sources.wind_fallback import FallbackWindClient

__all__ = [
    "AirNowClient",
    "BBox",
    "DEFAULT_SOURCES",
    "DataSource",
    "FallbackWindClient",
    "FirmsClient",
    "MetNorwayClient",
    "OpenAQClient",
    "OpenMeteoAirQualityClient",
    "OpenMeteoClient",
    "SourceResult",
    "TimeRange",
]
