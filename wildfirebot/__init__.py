"""WildFireBot: a screening forecast of wildfire smoke and PM2.5 AQI.

``build_forecast`` is loaded lazily so ``python -m wildfirebot.forecast`` does
not import this module twice.
"""

__all__ = ["build_forecast", "__version__"]

__version__ = "0.1.0"


def __getattr__(name: str):
    if name == "build_forecast":
        from wildfirebot.forecast import build_forecast

        return build_forecast
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
