"""Great-circle geometry for one fire and one target location."""

from __future__ import annotations

import math

# Mean Earth radius. Distance and bearing both use this sphere.
EARTH_RADIUS_KM = 6371.0
KM_PER_DEG_LAT = EARTH_RADIUS_KM * math.pi / 180.0

_COMPASS_POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two WGS84 points."""

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_KM * c


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees [0, 360).

    0 is north, 90 is east. Identical points return 0 because the bearing is undefined.
    """

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    east = math.sin(d_lambda) * math.cos(phi2)
    north = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        d_lambda
    )
    return math.degrees(math.atan2(east, north)) % 360.0


def angular_separation_deg(a_deg: float, b_deg: float) -> float:
    """Smallest angle between two bearings, in degrees [0, 180]."""

    return abs((a_deg - b_deg + 180.0) % 360.0 - 180.0)


def compass_point(bearing_deg_: float) -> str:
    """16-point compass label for a bearing in degrees."""

    index = int((bearing_deg_ % 360.0) / 22.5 + 0.5) % 16
    return _COMPASS_POINTS[index]


def format_coord(value: float) -> str:
    """Format a coordinate without scientific notation or trailing zeros."""

    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def bounding_boxes(
    lat: float, lon: float, radius_km: float
) -> list[tuple[float, float, float, float]]:
    """Return one or two ``(west, south, east, north)`` boxes covering the radius.

    A radius that crosses the antimeridian becomes two boxes so each one has
    ``west <= east``. Latitude is clamped to the FIRMS world extent.
    """

    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    lat = min(90.0, max(-90.0, lat))
    lon = ((lon + 180.0) % 360.0) - 180.0
    d_lat = radius_km / KM_PER_DEG_LAT
    cos_lat = math.cos(math.radians(min(89.0, max(-89.0, lat))))
    d_lon = min(180.0, radius_km / (KM_PER_DEG_LAT * cos_lat))
    south = max(-90.0, lat - d_lat)
    north = min(90.0, lat + d_lat)
    west = lon - d_lon
    east = lon + d_lon
    if west < -180.0:
        return [(west + 360.0, south, 180.0, north), (-180.0, south, east, north)]
    if east > 180.0:
        return [(west, south, 180.0, north), (-180.0, south, east - 360.0, north)]
    return [(west, south, east, north)]


def format_box(west: float, south: float, east: float, north: float) -> str:
    """FIRMS area string ``west,south,east,north``."""

    return f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}"
