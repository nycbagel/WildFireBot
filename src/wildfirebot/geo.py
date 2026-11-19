"""Great-circle helpers. Distances use a spherical Earth of radius 6371 km."""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0

COMPASS_POINTS = (
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


@dataclass(frozen=True)
class BBox:
    """Closed longitude/latitude box. West must be less than east (no antimeridian wrap)."""

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        for name, value in (
            ("west", self.west),
            ("east", self.east),
        ):
            if not -180 <= value <= 180:
                raise ValueError(f"{name} longitude {value} is outside -180..180")
        for name, value in (("south", self.south), ("north", self.north)):
            if not -90 <= value <= 90:
                raise ValueError(f"{name} latitude {value} is outside -90..90")
        if self.west >= self.east:
            raise ValueError("bbox west must be < east; antimeridian boxes are not supported yet")
        if self.south >= self.north:
            raise ValueError("bbox south must be < north")

    @property
    def centroid(self) -> tuple[float, float]:
        """Return (latitude, longitude) of the box center."""
        return ((self.south + self.north) / 2.0, (self.west + self.east) / 2.0)

    def contains(self, latitude: float, longitude: float) -> bool:
        return self.south <= latitude <= self.north and self.west <= longitude <= self.east


def bbox_from_point(latitude: float, longitude: float, radius_km: float) -> BBox:
    """Axis-aligned box that contains the circle of `radius_km` around the point."""
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    delta_lat = radius_km / 111.0
    cos_lat = max(0.1, abs(math.cos(math.radians(latitude))))
    delta_lon = radius_km / (111.0 * cos_lat)
    west = longitude - delta_lon
    east = longitude + delta_lon
    if west < -180 or east > 180:
        raise ValueError(
            "search box crosses the antimeridian; milestone 1 only supports boxes that stay within -180..180"
        )
    return BBox(
        west=west,
        south=max(-90.0, latitude - delta_lat),
        east=east,
        north=min(90.0, latitude + delta_lat),
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_m(lat1, lon1, lat2, lon2) / 1000.0


def destination(
    latitude: float,
    longitude: float,
    bearing_deg: float,
    distance_m: float,
) -> tuple[float, float]:
    """Point reached by traveling `distance_m` along `bearing_deg` (0 = north, clockwise)."""
    if distance_m == 0:
        return latitude, longitude
    delta = distance_m / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(theta)
    )
    lon2 = lon1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(lat1),
        math.cos(delta) - math.sin(lat1) * math.sin(lat2),
    )
    longitude_deg = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    return math.degrees(lat2), longitude_deg


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing in degrees from point 1 to point 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    east = math.sin(dlambda) * math.cos(phi2)
    north = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(east, north)) + 360.0) % 360.0


def compass_point(bearing_deg: float) -> str:
    index = int((bearing_deg + 11.25) // 22.5) % 16
    return COMPASS_POINTS[index]
