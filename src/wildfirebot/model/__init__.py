"""Air-quality calculations. The trained PM2.5 model arrives in milestone 2."""

from wildfirebot.model.aqi import AQIResult, category_for_aqi, pm25_to_aqi, predict_pm25

__all__ = ["AQIResult", "category_for_aqi", "pm25_to_aqi", "predict_pm25"]
