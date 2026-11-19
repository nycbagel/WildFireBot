"""Group nearby hotspots and summarize each group by FRP."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from wildfirebot.geo import haversine_km
from wildfirebot.sources.base import optional_float

_LEVEL_ORDER = {"URT": 0, "RT": 1, "NRT": 2, "SP": 3}


@dataclass(frozen=True)
class Cluster:
    latitude: float
    longitude: float
    frp_mw: float
    count: int
    sources: tuple[str, ...]
    processing_levels: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    satellites: tuple[str, ...]


def cluster_hotspots(frame: pd.DataFrame, radius_km: float) -> list[Cluster]:
    """Single-linkage clusters: hotspots within `radius_km` of each other join.

    The centroid is weighted by FRP. Rows with no FRP (Landsat) still join a cluster
    but do not move a centroid that already has FRP, and they do not add to `frp_mw`.
    """
    if radius_km <= 0:
        raise ValueError("radius_km must be positive")
    if frame.empty:
        return []
    rows = frame.to_dict("records")
    count = len(rows)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    mean_lat = sum(float(row["latitude"]) for row in rows) / count
    cos_lat = max(0.2, math.cos(math.radians(mean_lat)))
    lat_step = radius_km / 111.0
    lon_step = radius_km / (111.0 * cos_lat)
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, row in enumerate(rows):
        cell = (
            math.floor(float(row["latitude"]) / lat_step),
            math.floor(float(row["longitude"]) / lon_step),
        )
        buckets.setdefault(cell, []).append(index)

    for index, row in enumerate(rows):
        cell_lat = math.floor(float(row["latitude"]) / lat_step)
        cell_lon = math.floor(float(row["longitude"]) / lon_step)
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                for other in buckets.get((cell_lat + dlat, cell_lon + dlon), []):
                    if other <= index:
                        continue
                    other_row = rows[other]
                    distance = haversine_km(
                        float(row["latitude"]),
                        float(row["longitude"]),
                        float(other_row["latitude"]),
                        float(other_row["longitude"]),
                    )
                    if distance <= radius_km:
                        union(index, other)

    groups: dict[int, list[dict]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(find(index), []).append(row)
    clusters = [_make_cluster(members) for members in groups.values()]
    clusters.sort(key=lambda item: (-item.frp_mw, -item.count, item.latitude, item.longitude))
    return clusters


def _make_cluster(members: list[dict]) -> Cluster:
    frps = [optional_float(row.get("frp")) for row in members]
    positive = [value for value in frps if value is not None and value > 0]
    if positive:
        weights = [value if value is not None and value > 0 else 0.0 for value in frps]
        frp_total = sum(positive)
    else:
        weights = [1.0] * len(members)
        frp_total = 0.0
    weight_sum = sum(weights)
    latitude = sum(
        weight * float(row["latitude"]) for weight, row in zip(weights, members, strict=True)
    )
    longitude = sum(
        weight * float(row["longitude"]) for weight, row in zip(weights, members, strict=True)
    )
    times = [_as_utc(row["acquired_at"]) for row in members]
    sources = tuple(sorted({str(row.get("source") or "") for row in members if row.get("source")}))
    levels = sorted(
        {str(row.get("processing_level") or "") for row in members if row.get("processing_level")},
        key=lambda level: _LEVEL_ORDER.get(level, 9),
    )
    satellites = tuple(
        sorted({str(row.get("satellite") or "") for row in members if row.get("satellite")})
    )
    return Cluster(
        latitude=latitude / weight_sum,
        longitude=longitude / weight_sum,
        frp_mw=frp_total,
        count=len(members),
        sources=sources,
        processing_levels=tuple(levels),
        first_seen=min(times),
        last_seen=max(times),
        satellites=satellites,
    )


def _as_utc(value: object) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = pd.Timestamp(value).to_pydatetime()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
