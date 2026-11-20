"""Air-quality calculations: EPA AQI conversion and the Gaussian smoke plume.

The trained PM2.5 model arrives in milestone 2.
"""

from wildfirebot.model.aqi import AQIResult, category_for_aqi, pm25_to_aqi, predict_pm25
from wildfirebot.model.plume import PlumeConfig, fire_pm25_at, plume_pm25

__all__ = [
    "AQIResult",
    "PlumeConfig",
    "category_for_aqi",
    "fire_pm25_at",
    "plume_pm25",
    "pm25_to_aqi",
    "predict_pm25",
]
