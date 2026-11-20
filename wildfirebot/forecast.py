"""Build a smoke outlook for one latitude and longitude.

Run it from the repository root::

    python -m wildfirebot.forecast --lat 37.87 --lon -122.27 --radius-km 150

The same work is ``build_forecast(lat, lon)``, which returns a JSON-ready dict
a chatbot can call in-process. ``--json-only`` prints that dict and nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from wildfirebot.aqi import aqi_from_pm25
from wildfirebot.clients.airnow import AirNowResult, fetch_airnow
from wildfirebot.clients.firms import FirmsResult, fetch_hotspots
from wildfirebot.clients.nws import HourlyWind, NwsResult, fetch_hourly_winds
from wildfirebot.clients.open_meteo import OpenMeteoResult, fetch_pm25_forecast
from wildfirebot.config import Settings, load_settings
from wildfirebot.geo import bearing_deg, compass_point, haversine_km
from wildfirebot.plume import (
    EMISSION_ASSUMPTION,
    ModelConfig,
    SmokeSource,
    arrival_hours,
    is_downwind,
    source_increment_ug_m3,
    wind_toward_deg,
)
from wildfirebot.summary import render_summary

HORIZONS_HOURS = (0, 6, 12, 24, 48)
MAX_FIRES_IN_OUTPUT = 50
MAX_CONTRIBUTORS = 8

_BASELINE_LABELS = {
    "airnow_concentration": "AirNow",
    "airnow_inverted_aqi": "AirNow AQI (inverted)",
    "open_meteo": "Open-Meteo",
    "airnow_held": "AirNow (held)",
}


def _publish_fire(fire: dict[str, object]) -> dict[str, object]:
    """Drop the working distance in meters and round the public fields."""

    return {
        "latitude": round(float(fire["latitude"]), 5),
        "longitude": round(float(fire["longitude"]), 5),
        "frp_mw": round(float(fire["frp_mw"]), 2),
        "confidence": fire["confidence"],
        "acq_date": fire["acq_date"],
        "acq_time": fire["acq_time"],
        "acq_time_display": fire["acq_time_display"],
        "distance_km": round(float(fire["distance_km"]), 2),
        "bearing_deg": round(float(fire["bearing_deg"]), 1),
        "direction_deg": round(float(fire["direction_deg"]), 1),
        "direction": fire["direction"],
        "downwind": fire["downwind"],
        "arrival_hours": fire["arrival_hours"],
    }


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _display_acq_time(value: str) -> str:
    text = str(value).strip()
    if len(text) == 4 and text.isdigit():
        return f"{text[:2]}:{text[2:]}"
    return text


def _wind_sample(period: HourlyWind | None, config: ModelConfig) -> dict[str, object] | None:
    if period is None:
        return None
    floored = period.speed_m_s < config.min_wind_speed_m_s
    speed = max(period.speed_m_s, config.min_wind_speed_m_s)
    toward = wind_toward_deg(period.from_deg)
    return {
        "from_deg": period.from_deg,
        "toward_deg": toward,
        "from_compass": compass_point(period.from_deg),
        "toward_compass": compass_point(toward),
        "speed_m_s": speed,
        "reported_speed_m_s": period.speed_m_s,
        "label": period.label,
        "floored": floored,
        "valid_start": period.start.isoformat(),
    }


def _publish_wind(wind: dict[str, object] | None) -> dict[str, object] | None:
    if wind is None:
        return None
    published = dict(wind)
    published["from_deg"] = round(float(wind["from_deg"]), 1)
    published["toward_deg"] = round(float(wind["toward_deg"]), 1)
    published["speed_m_s"] = round(float(wind["speed_m_s"]), 3)
    published["reported_speed_m_s"] = round(float(wind["reported_speed_m_s"]), 3)
    return published


def _baseline(
    hours: int,
    when: datetime,
    airnow: AirNowResult,
    meteo: OpenMeteoResult,
) -> tuple[float | None, str | None]:
    """Choose the PM2.5 baseline for one horizon.

    Hour 0 prefers the AirNow current reading. Later hours use the Open-Meteo
    hourly forecast. Open-Meteo fills in whenever AirNow is missing, and a
    current AirNow value is held forward when Open-Meteo is missing.
    """

    meteo_value = meteo.at(when) if meteo.status == "ok" else None
    airnow_value = airnow.pm25_ug_m3
    if hours == 0 and airnow_value is not None:
        if airnow.pm25_from == "inverted_aqi":
            return airnow_value, "airnow_inverted_aqi"
        return airnow_value, "airnow_concentration"
    if meteo_value is not None:
        return meteo_value, "open_meteo"
    if airnow_value is not None:
        return airnow_value, "airnow_held"
    return None, None


def _assess_fires(
    fires: list[dict[str, object]],
    wind: dict[str, object] | None,
    config: ModelConfig,
) -> None:
    """Attach downwind and arrival, using the current hour's wind, to each fire."""

    for fire in fires:
        if wind is None:
            fire["downwind"] = None
            fire["arrival_hours"] = None
            continue
        downwind = float(fire["distance_m"]) <= 1.0 or is_downwind(
            float(fire["bearing_deg"]),
            float(wind["from_deg"]),
            config.downwind_half_angle_deg,
        )
        fire["downwind"] = downwind
        fire["arrival_hours"] = _round(
            arrival_hours(float(fire["distance_m"]), float(wind["speed_m_s"])),
            2,
        )


def _horizon_block(
    hours: int,
    when: datetime,
    fires: list[dict[str, object]],
    wind: dict[str, object] | None,
    baseline: float | None,
    baseline_source: str | None,
    config: ModelConfig,
) -> dict[str, object]:
    contributors: list[dict[str, object]] = []
    downwind_count = 0
    smoke: float | None
    if wind is None:
        smoke = None
    else:
        smoke = 0.0
        for fire in fires:
            source = SmokeSource(
                distance_m=float(fire["distance_m"]),
                bearing_deg=float(fire["bearing_deg"]),
                frp_mw=float(fire["frp_mw"]),
            )
            downwind = is_downwind(
                source.bearing_deg, float(wind["from_deg"]), config.downwind_half_angle_deg
            ) or source.distance_m <= 1.0
            if downwind:
                downwind_count += 1
            increment = source_increment_ug_m3(
                source,
                float(wind["from_deg"]),
                float(wind["speed_m_s"]),
                float(hours),
                config,
            )
            if increment <= 0:
                continue
            smoke += increment
            contributors.append(
                {
                    "latitude": round(float(fire["latitude"]), 5),
                    "longitude": round(float(fire["longitude"]), 5),
                    "distance_km": round(float(fire["distance_km"]), 2),
                    "frp_mw": round(float(fire["frp_mw"]), 2),
                    "arrival_hours": _round(
                        arrival_hours(source.distance_m, float(wind["speed_m_s"])),
                        2,
                    ),
                    "smoke_pm25_ug_m3": round(increment, 2),
                }
            )
    contributors.sort(key=lambda item: float(item["smoke_pm25_ug_m3"]), reverse=True)
    truncated = len(contributors) > MAX_CONTRIBUTORS
    total = None if baseline is None or smoke is None else baseline + smoke
    aqi_block: dict[str, object] | None = None
    if total is not None:
        aqi_block = aqi_from_pm25(max(0.0, total)).as_dict()
    return {
        "hours": hours,
        "time": when.isoformat(),
        "wind": _publish_wind(wind),
        "baseline_pm25_ug_m3": _round(baseline, 2),
        "baseline_source": None if baseline_source is None else _BASELINE_LABELS[baseline_source],
        "smoke_pm25_ug_m3": _round(smoke, 2),
        "total_pm25_ug_m3": _round(total, 2),
        "aqi": None if aqi_block is None else aqi_block["aqi"],
        "category": None if aqi_block is None else aqi_block["category"],
        "health_message": None if aqi_block is None else aqi_block["health_message"],
        "downwind_fire_count": downwind_count,
        "arrived_fire_count": len(contributors),
        "contributors": contributors[:MAX_CONTRIBUTORS],
        "contributors_truncated": truncated,
    }


def _notes(
    firms: FirmsResult,
    nws: NwsResult,
    airnow: AirNowResult,
    meteo: OpenMeteoResult,
    horizons: list[dict[str, object]],
    fire_count: int,
) -> list[str]:
    notes: list[str] = []
    if firms.status == "skipped":
        notes.append(firms.detail + " The outlook is the baseline air quality only.")
    elif firms.status == "error":
        notes.append(firms.detail)
    if nws.status != "ok":
        notes.append(nws.detail + " Downwind, arrival, and the smoke increment were not estimated.")
    if airnow.status == "skipped":
        notes.append(airnow.detail + " Open-Meteo is the PM2.5 baseline when it is available.")
    elif airnow.status == "error":
        notes.append(airnow.detail + " Open-Meteo is the fallback baseline when it is available.")
    elif airnow.pm25_from == "inverted_aqi":
        notes.append(
            "AirNow did not include a PM2.5 concentration. The NowCast AQI was converted "
            "with the 2024 breakpoints, which is only a stand-in for the missing micrograms."
        )
    if meteo.status != "ok":
        notes.append(meteo.detail)
    if any(isinstance(item.get("wind"), dict) and item["wind"].get("floored") for item in horizons):
        notes.append(
            "At least one horizon had wind slower than the 0.5 m/s floor. "
            "That floor keeps travel time and concentration finite."
        )
    if any(item.get("baseline_source") == _BASELINE_LABELS["airnow_held"] for item in horizons):
        notes.append(
            "Open-Meteo had no PM2.5 for at least one later horizon, so the AirNow "
            "current value was held constant there."
        )
    if fire_count >= 8:
        notes.append(
            "Adjacent FIRMS pixels are summed as separate point sources, so a large "
            "fire footprint can be over-counted."
        )
    pm_forecasts = [
        item
        for item in airnow.forecasts
        if "PM2.5" in item.parameter.upper() or item.parameter.upper().replace(".", "") == "PM25"
    ]
    if pm_forecasts:
        first = pm_forecasts[0]
        where = f" for {first.reporting_area}" if first.reporting_area else ""
        when = f" on {first.date}" if first.date else ""
        category = f" ({first.category})" if first.category else ""
        notes.append(
            f"AirNow's own forecast{where}{when} is PM2.5 AQI {first.aqi}{category}. "
            "The horizons in this outlook are a separate smoke-adjusted estimate."
        )
    return notes


def build_forecast(
    lat: float,
    lon: float,
    radius_km: float = 150.0,
    *,
    source: str = "VIIRS_NOAA20_NRT",
    horizons_hours: tuple[int, ...] = HORIZONS_HOURS,
    config: ModelConfig | None = None,
    settings: Settings | None = None,
    transport=None,
    now: datetime | None = None,
    max_fires: int = MAX_FIRES_IN_OUTPUT,
) -> dict[str, object]:
    """Return a JSON-ready smoke outlook for one location.

    Missing API keys skip that source and are described in ``notes``. Network
    failures are reported the same way. The other sources still run.
    """

    if not -90.0 <= lat <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    if any(hours < 0 for hours in horizons_hours):
        raise ValueError("horizons must be zero or positive")
    model = (config or ModelConfig()).validated()
    runtime = settings or load_settings()
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    else:
        clock = clock.astimezone(timezone.utc)

    firms = fetch_hotspots(
        lat,
        lon,
        radius_km,
        map_key=runtime.firms_map_key,
        source=source,
        user_agent=runtime.nws_user_agent,
        transport=transport,
    )
    nws = fetch_hourly_winds(
        lat,
        lon,
        user_agent=runtime.nws_user_agent,
        transport=transport,
    )
    airnow = fetch_airnow(
        lat,
        lon,
        api_key=runtime.airnow_api_key,
        user_agent=runtime.nws_user_agent,
        transport=transport,
    )
    meteo = fetch_pm25_forecast(
        lat,
        lon,
        user_agent=runtime.nws_user_agent,
        transport=transport,
    )

    fires: list[dict[str, object]] = []
    for hotspot in firms.hotspots:
        distance_km = haversine_km(lat, lon, hotspot.lat, hotspot.lon)
        if distance_km > radius_km:
            continue
        # bearing_deg is from the fire to the target. That is the angle the
        # downwind test compares with the wind's direction of travel.
        # direction is where the fire sits as seen from the target, which is
        # what the plain-language readout means by "to the northeast".
        bearing_to_target = bearing_deg(hotspot.lat, hotspot.lon, lat, lon)
        direction_to_fire = bearing_deg(lat, lon, hotspot.lat, hotspot.lon)
        fires.append(
            {
                "latitude": hotspot.lat,
                "longitude": hotspot.lon,
                "frp_mw": hotspot.frp,
                "confidence": hotspot.confidence,
                "acq_date": hotspot.acq_date,
                "acq_time": hotspot.acq_time,
                "acq_time_display": _display_acq_time(hotspot.acq_time),
                "distance_km": distance_km,
                "distance_m": distance_km * 1000.0,
                "bearing_deg": bearing_to_target,
                "direction_deg": direction_to_fire,
                "direction": compass_point(direction_to_fire),
            }
        )
    fires.sort(key=lambda item: (float(item["distance_km"]), -float(item["frp_mw"])))
    fire_count = len(fires)

    current_period = nws.at(clock) if nws.status == "ok" else None
    current_wind = _wind_sample(current_period, model)
    _assess_fires(fires, current_wind, model)

    horizons: list[dict[str, object]] = []
    for hours in horizons_hours:
        when = clock + timedelta(hours=hours)
        period = nws.at(when) if nws.status == "ok" else None
        wind = _wind_sample(period, model)
        baseline, baseline_source = _baseline(hours, when, airnow, meteo)
        horizons.append(
            _horizon_block(hours, when, fires, wind, baseline, baseline_source, model)
        )

    published = [_publish_fire(fire) for fire in fires[:max_fires]]

    return {
        "location": {
            "latitude": lat,
            "longitude": lon,
            "radius_km": radius_km,
        },
        "generated_at": clock.isoformat(),
        "model": {
            "emission_factor_kg_per_mj": model.emission_factor_kg_per_mj,
            "emission_assumption": EMISSION_ASSUMPTION,
            "stability_class": model.stability,
            "dispersion": "Briggs 1973 rural Pasquill-Gifford sigmas",
            "plume_height_m": model.plume_height_m,
            "downwind_half_angle_deg": model.downwind_half_angle_deg,
            "min_wind_speed_m_s": model.min_wind_speed_m_s,
            "smoke_estimate": (
                "Ground-level centerline concentration for fires inside the downwind "
                "sector, added once the travel time has elapsed. Fires outside the "
                "sector contribute 0."
            ),
        },
        "sources": {
            "firms": {
                "status": firms.status,
                "detail": firms.detail,
                "source": firms.source,
                "fetched": len(firms.hotspots),
                "in_radius": fire_count,
            },
            "nws": {"status": nws.status, "detail": nws.detail},
            "airnow": {
                "status": airnow.status,
                "detail": airnow.detail,
                "pm25_ug_m3": _round(airnow.pm25_ug_m3, 2),
                "pm25_from": airnow.pm25_from,
                "aqi": airnow.aqi,
                "category": airnow.category,
                "reporting_area": airnow.reporting_area,
                "forecasts": [item.as_dict() for item in airnow.forecasts],
            },
            "open_meteo": {"status": meteo.status, "detail": meteo.detail},
        },
        "fire_count": fire_count,
        "fires": published,
        "horizons": horizons,
        "notes": _notes(firms, nws, airnow, meteo, horizons, fire_count),
    }


def _parse_horizons(text: str) -> tuple[int, ...]:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("provide at least one horizon, for example 0,6,12,24,48")
    try:
        values = tuple(int(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizons must be comma-separated hours") from exc
    if any(value < 0 for value in values):
        raise argparse.ArgumentTypeError("horizons must be zero or positive")
    return values


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m wildfirebot.forecast",
        description=(
            "Estimate how active fires near a location could change PM2.5 and the AQI "
            "over the next two days."
        ),
    )
    parser.add_argument("--lat", type=float, required=True, help="Target latitude")
    parser.add_argument("--lon", type=float, required=True, help="Target longitude")
    parser.add_argument(
        "--radius-km",
        type=float,
        default=150.0,
        help="Search radius around the target in kilometers (default: 150)",
    )
    parser.add_argument(
        "--source",
        default="VIIRS_NOAA20_NRT",
        help="FIRMS source: VIIRS_NOAA20_NRT, VIIRS_NOAA21_NRT, VIIRS_SNPP_NRT, or MODIS_NRT",
    )
    parser.add_argument(
        "--horizons",
        type=_parse_horizons,
        default=HORIZONS_HOURS,
        help="Comma-separated forecast hours (default: 0,6,12,24,48)",
    )
    parser.add_argument(
        "--plume-height-m",
        type=float,
        default=500.0,
        help="Effective plume height in meters (default: 500)",
    )
    parser.add_argument(
        "--emission-factor",
        type=float,
        default=0.02,
        help="PM2.5 kilograms per megajoule of FRP (default: 0.02)",
    )
    parser.add_argument(
        "--stability",
        default="D",
        help="Pasquill stability class A-F (default: D, neutral)",
    )
    parser.add_argument(
        "--downwind-deg",
        type=float,
        default=30.0,
        help="Half-width of the downwind sector in degrees (default: 30)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Print the JSON payload only, for a chatbot to capture",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    settings: Settings | None = None,
    transport=None,
    now: datetime | None = None,
) -> int:
    """CLI entry point. Returns a process status code."""

    try:
        args = parse_args(argv)
        config = ModelConfig(
            emission_factor_kg_per_mj=args.emission_factor,
            plume_height_m=args.plume_height_m,
            stability=args.stability,
            downwind_half_angle_deg=args.downwind_deg,
        ).validated()
        result = build_forecast(
            args.lat,
            args.lon,
            args.radius_km,
            source=args.source,
            horizons_hours=args.horizons,
            config=config,
            settings=settings,
            transport=transport,
            now=now,
        )
    except (ValueError, argparse.ArgumentError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    payload = json.dumps(result, indent=2, allow_nan=False)
    if args.json_only:
        print(payload)
    else:
        print(render_summary(result), end="")
        print()
        print("--- json ---")
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
