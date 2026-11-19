"""Back-trajectory smoke-exposure score.

For each forecast hour the air arriving at the target is walked upwind, one hour
at a time, using the wind at the target (a spatially uniform wind is a milestone-1
limit). A fire counts when that path gets at least `approach_margin_km` closer to
it than the target already is. Fires within `local_radius_km` count even in calm
or outbound wind, because people standing next to the fire are already in it.

The score is dimensionless. A 100 MW cluster on the trajectory, under an 800 m
boundary layer, detected at the forecast hour, scores about 1. It is not µg/m³
and it is not an AQI.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from wildfirebot.fires.cluster import Cluster
from wildfirebot.geo import destination, haversine_km


@dataclass(frozen=True)
class ScoreConfig:
    length_scale_km: float = 40.0
    local_radius_km: float = 5.0
    reference_blh_m: float = 800.0
    min_blh_m: float = 200.0
    max_blh_factor: float = 4.0
    frp_scale_mw: float = 100.0
    persistence_hours: float = 36.0
    approach_margin_km: float = 1.0


def exposure_label(score: float) -> str:
    """Plain-language bucket for a score. Not an EPA AQI category."""
    if score < 0.3:
        return "little expected smoke"
    if score < 1.5:
        return "light smoke possible"
    if score < 4.0:
        return "moderate smoke possible"
    return "heavy smoke possible"


def trajectory_points(
    latitude: float,
    longitude: float,
    steps: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Walk upwind. Each step is (speed m/s, meteorological direction the wind is from).

    The first point is the target. Later points are where that air was one hour earlier.
    """
    points = [(latitude, longitude)]
    for speed, direction in steps:
        latitude, longitude = destination(latitude, longitude, direction, speed * 3600.0)
        points.append((latitude, longitude))
    return points


def blh_factor(blh_m: float | None, config: ScoreConfig) -> tuple[float, str | None]:
    """Shallower mixed layers concentrate smoke. Missing height stays neutral."""
    if blh_m is None or blh_m <= 0:
        return 1.0, "boundary layer height missing; dilution factor set to 1"
    factor = config.reference_blh_m / max(blh_m, config.min_blh_m)
    return min(config.max_blh_factor, factor), None


def score_clusters(
    target: tuple[float, float],
    points: list[tuple[float, float]],
    clusters: list[Cluster],
    when: datetime,
    blh_m: float | None,
    config: ScoreConfig | None = None,
) -> tuple[float, list[tuple[int, float]], str | None]:
    """Return the total score, per-cluster contributions, and a boundary-layer note."""
    settings = config or ScoreConfig()
    factor, note = blh_factor(blh_m, settings)
    parts: list[tuple[int, float]] = []
    total = 0.0
    for index, cluster in enumerate(clusters):
        if cluster.frp_mw <= 0:
            continue
        weight = (
            (cluster.frp_mw / settings.frp_scale_mw)
            * _proximity(target, cluster, points, settings)
            * factor
            * _persistence(when, cluster.last_seen, settings)
        )
        if weight > 0:
            parts.append((index, weight))
            total += weight
    parts.sort(key=lambda item: -item[1])
    return total, parts, note


def _proximity(
    target: tuple[float, float],
    cluster: Cluster,
    points: list[tuple[float, float]],
    config: ScoreConfig,
) -> float:
    distance_to_target = haversine_km(target[0], target[1], cluster.latitude, cluster.longitude)
    if distance_to_target <= config.local_radius_km:
        return 1.0
    if len(points) < 2:
        return 0.0
    distance_to_path = min(
        haversine_km(latitude, longitude, cluster.latitude, cluster.longitude)
        for latitude, longitude in points[1:]
    )
    if distance_to_path + config.approach_margin_km >= distance_to_target:
        return 0.0
    return math.exp(-distance_to_path / config.length_scale_km)


def _persistence(when: datetime, last_seen: datetime, config: ScoreConfig) -> float:
    age_hours = (when - last_seen).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0.0
    return math.exp(-age_hours / config.persistence_hours)
