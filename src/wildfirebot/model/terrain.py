"""Terrain and stability adjustment for the Gaussian plume. No API key (Open-Meteo).

`terrain_adjustment(fire_lat, fire_lon, tgt_lat, tgt_lon)` returns a dict with
`blocking_factor` (0.1 to 1, multiplies the plume concentration), `stability_class`
(Pasquill-Gifford A-F from wind, sun, cloud, and a 2 m / 120 m inversion check),
`mixing_height_m` (boundary-layer height that caps vertical spread), and `notes`.
If either API fails it falls back to 1.0, class D, and 800 m and says so in `notes`.
"""

from __future__ import annotations

import json
import math
import urllib.request
from urllib.parse import urlencode

from wildfirebot.geo import haversine_km

ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT = {"blocking_factor": 1.0, "stability_class": "D", "mixing_height_m": 800.0}


def _get_json(url: str, params: dict, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(
        f"{url}?{urlencode(params)}", headers={"User-Agent": "wildfirebot"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _profile(fire: tuple[float, float], target: tuple[float, float], n: int = 50) -> list[float]:
    points = [
        (
            fire[0] + (target[0] - fire[0]) * i / (n - 1),
            fire[1] + (target[1] - fire[1]) * i / (n - 1),
        )
        for i in range(n)
    ]
    body = _get_json(
        ELEVATION_URL,
        {
            "latitude": ",".join(f"{p[0]:.5f}" for p in points),
            "longitude": ",".join(f"{p[1]:.5f}" for p in points),
        },
    )
    return [float(value) for value in body["elevation"]]


def stability_class(wind_m_s: float, solar_w_m2: float, cloud_pct: float, is_day: bool) -> str:
    """Pasquill-Gifford class from the standard Turner-style table."""
    if is_day:
        strength = 3 if solar_w_m2 > 600 else 2 if solar_w_m2 > 300 else 1
        table = {3: "AABCC", 2: "ABBCD", 1: "BCCDD"}[strength]
    else:
        table = "FFEDD" if cloud_pct < 50 else "EEDDD"
    column = 0 if wind_m_s < 2 else 1 if wind_m_s < 3 else 2 if wind_m_s < 5 else 3
    if wind_m_s >= 6:
        column = 4
    return table[column]


def terrain_adjustment(
    fire_lat: float,
    fire_lon: float,
    tgt_lat: float,
    tgt_lon: float,
    plume_height_m: float = 200.0,
) -> dict:
    out: dict = dict(DEFAULT)
    notes: list[str] = []
    try:
        elevations = _profile((fire_lat, fire_lon), (tgt_lat, tgt_lon))
        n = len(elevations)
        top = elevations[0] + plume_height_m
        worst = max(
            elevations[i] - (top + (elevations[-1] - top) * i / (n - 1)) for i in range(1, n - 1)
        )
        if worst > 0:
            share = min(1.0, worst / (2 * plume_height_m))
            out["blocking_factor"] = round(max(0.1, 1 - share * 0.9), 2)
            notes.append(f"ridge {worst:.0f} m above plume line, smoke partly blocked")
        tail = elevations[int(n * 0.6) :]
        basin = max(tail) - elevations[-1] > 100 and elevations[-1] <= min(tail) + 30
        out.update(
            fire_elev_m=elevations[0],
            target_elev_m=elevations[-1],
            max_elev_m=max(elevations),
            basin=basin,
            distance_km=round(haversine_km(fire_lat, fire_lon, tgt_lat, tgt_lon), 1),
        )
        if basin:
            notes.append("target in a basin or valley, smoke may pool")
    except Exception as exc:  # noqa: BLE001 - any failure falls back to no blocking
        notes.append(f"elevation failed ({exc.__class__.__name__}); blocking=1.0")
    try:
        weather = _get_json(
            FORECAST_URL,
            {
                "latitude": tgt_lat,
                "longitude": tgt_lon,
                "wind_speed_unit": "ms",
                "timezone": "UTC",
                "current": "wind_speed_10m,shortwave_radiation,cloud_cover,is_day",
                "hourly": "boundary_layer_height,temperature_2m,temperature_120m",
                "forecast_days": 1,
            },
        )
        current, hourly = weather["current"], weather["hourly"]
        cls = stability_class(
            current["wind_speed_10m"],
            current["shortwave_radiation"],
            current["cloud_cover"],
            bool(current["is_day"]),
        )
        inversion = hourly["temperature_120m"][0] - hourly["temperature_2m"][0]
        if inversion > 0.5 and cls in "ABCD":
            cls = "E"
            notes.append("inversion (T120 > T2), stable class E")
        out["stability_class"] = cls
        out["mixing_height_m"] = max(float(hourly["boundary_layer_height"][0]), 50.0)
        if out.get("basin") and (cls in "EF" or inversion > 0.5):
            out["blocking_factor"] = min(1.0, out["blocking_factor"] * 1.5)
            notes.append("basin and stable air: trapping boost x1.5 (capped at 1)")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"weather failed ({exc.__class__.__name__}); class D, 800 m")
    if not math.isfinite(out["mixing_height_m"]):
        out["mixing_height_m"] = DEFAULT["mixing_height_m"]
    out["notes"] = notes
    return out
