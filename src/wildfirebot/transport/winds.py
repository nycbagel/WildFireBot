"""Turn an hourly wind table into a complete series, filling only short gaps.

Direction is interpolated in vector components so 350° and 10° meet near north,
not at 180°. A gap longer than `max_gap_hours` is left missing and flagged.
When the 10 m wind is still missing and the 850 hPa wind is present, the 850 hPa
wind is used for transport and marked as a substitution.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from wildfirebot.sources.base import floor_hour, optional_float

MAX_SPEED_M_S = 100.0
MAX_BLH_M = 10_000.0


@dataclass(frozen=True)
class WindHour:
    time: datetime
    speed_10m: float | None
    direction_10m: float | None
    speed_850: float | None
    direction_850: float | None
    blh_m: float | None
    interpolated_10m: bool
    interpolated_850: bool
    interpolated_blh: bool
    transport_speed: float | None
    transport_direction: float | None
    transport_source: str
    backfilled: bool = False  # 10 m wind copied back from the first forecast hour (estimate)


@dataclass(frozen=True)
class WindSummary:
    hours: int
    speed_10m_mean: float | None
    direction_10m_prevailing: float | None
    speed_850_mean: float | None
    direction_850_prevailing: float | None
    blh_mean: float | None
    blh_min: float | None
    blh_max: float | None
    interpolated_hours: int
    missing_transport_hours: int
    substituted_hours: int


def wind_components(speed: float, direction_from_deg: float) -> tuple[float, float]:
    """Eastward and northward components of where the wind is blowing toward."""
    theta = math.radians(direction_from_deg)
    return -math.sin(theta) * speed, -math.cos(theta) * speed


def speed_and_direction(u_east: float, v_north: float) -> tuple[float, float]:
    speed = math.hypot(u_east, v_north)
    if speed == 0:
        return 0.0, 0.0
    bearing_toward = math.degrees(math.atan2(u_east, v_north))
    direction_from = (bearing_toward + 180.0) % 360.0
    return speed, direction_from


def prepare_winds(frame: pd.DataFrame, *, max_gap_hours: int = 3) -> list[WindHour]:
    if max_gap_hours < 0:
        raise ValueError("max_gap_hours cannot be negative")
    if frame.empty:
        return []
    by_hour: dict[datetime, dict] = {}
    for row in frame.to_dict("records"):
        moment = row.get("time")
        if moment is None or (isinstance(moment, float) and math.isnan(moment)):
            continue
        stamp = pd.Timestamp(moment)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        hour = floor_hour(stamp.to_pydatetime())
        by_hour.setdefault(hour, row)
    if not by_hour:
        return []
    start = min(by_hour)
    end = max(by_hour)
    hours = _hours_between(start, end)

    speed_10 = [_clean_speed(_value(by_hour.get(hour), "wind_speed_10m")) for hour in hours]
    dir_10 = [_clean_direction(_value(by_hour.get(hour), "wind_direction_10m")) for hour in hours]
    speed_850 = [_clean_speed(_value(by_hour.get(hour), "wind_speed_850hPa")) for hour in hours]
    dir_850 = [
        _clean_direction(_value(by_hour.get(hour), "wind_direction_850hPa")) for hour in hours
    ]
    blh = [_clean_blh(_value(by_hour.get(hour), "boundary_layer_height")) for hour in hours]

    u10, v10, interp_10 = _fill_vectors(speed_10, dir_10, max_gap_hours)
    u850, v850, interp_850 = _fill_vectors(speed_850, dir_850, max_gap_hours)
    blh_filled, interp_blh = _fill_scalars(blh, max_gap_hours)

    prepared: list[WindHour] = []
    for index, hour in enumerate(hours):
        surface = _from_components(u10[index], v10[index])
        aloft = _from_components(u850[index], v850[index])
        if surface is not None:
            transport_speed, transport_direction = surface
            transport_source = "10m"
        elif aloft is not None:
            transport_speed, transport_direction = aloft
            transport_source = "850hPa"
        else:
            transport_speed, transport_direction = None, None
            transport_source = "missing"
        prepared.append(
            WindHour(
                time=hour,
                speed_10m=None if surface is None else surface[0],
                direction_10m=None if surface is None else surface[1],
                speed_850=None if aloft is None else aloft[0],
                direction_850=None if aloft is None else aloft[1],
                blh_m=blh_filled[index],
                interpolated_10m=interp_10[index],
                interpolated_850=interp_850[index],
                interpolated_blh=interp_blh[index],
                transport_speed=transport_speed,
                transport_direction=transport_direction,
                transport_source=transport_source,
                backfilled=bool(by_hour.get(hour, {}).get("wind_backfilled") is True),
            )
        )
    return prepared


def summarize_winds(hours: list[WindHour]) -> WindSummary:
    surface = [
        (hour.speed_10m, hour.direction_10m)
        for hour in hours
        if hour.speed_10m is not None and hour.direction_10m is not None
    ]
    aloft = [
        (hour.speed_850, hour.direction_850)
        for hour in hours
        if hour.speed_850 is not None and hour.direction_850 is not None
    ]
    blh_values = [hour.blh_m for hour in hours if hour.blh_m is not None]
    surface_mean, surface_dir = _mean_vector(surface)
    aloft_mean, aloft_dir = _mean_vector(aloft)
    return WindSummary(
        hours=len(hours),
        speed_10m_mean=surface_mean,
        direction_10m_prevailing=surface_dir,
        speed_850_mean=aloft_mean,
        direction_850_prevailing=aloft_dir,
        blh_mean=(sum(blh_values) / len(blh_values)) if blh_values else None,
        blh_min=min(blh_values) if blh_values else None,
        blh_max=max(blh_values) if blh_values else None,
        interpolated_hours=sum(
            1
            for hour in hours
            if hour.interpolated_10m or hour.interpolated_850 or hour.interpolated_blh
        ),
        missing_transport_hours=sum(1 for hour in hours if hour.transport_source == "missing"),
        substituted_hours=sum(1 for hour in hours if hour.transport_source == "850hPa"),
    )


def _hours_between(start: datetime, end: datetime) -> list[datetime]:
    hours = []
    cursor = start
    while cursor <= end:
        hours.append(cursor)
        cursor += timedelta(hours=1)
    return hours


def _value(row: dict | None, key: str) -> object:
    if row is None:
        return None
    return row.get(key)


def _clean_speed(value: object) -> float | None:
    number = optional_float(value)
    if number is None or number < 0 or number > MAX_SPEED_M_S:
        return None
    return number


def _clean_direction(value: object) -> float | None:
    number = optional_float(value)
    if number is None:
        return None
    if number == 360:
        return 0.0
    if number < 0 or number > 360:
        return None
    return number


def _clean_blh(value: object) -> float | None:
    number = optional_float(value)
    if number is None or number < 0 or number > MAX_BLH_M:
        return None
    return number


def _fill_vectors(
    speeds: list[float | None],
    directions: list[float | None],
    max_gap: int,
) -> tuple[list[float | None], list[float | None], list[bool]]:
    east: list[float | None] = []
    north: list[float | None] = []
    for speed, direction in zip(speeds, directions, strict=True):
        if speed is None or direction is None:
            east.append(None)
            north.append(None)
        else:
            u_east, v_north = wind_components(speed, direction)
            east.append(u_east)
            north.append(v_north)
    filled_east, interpolated = _fill_scalars(east, max_gap)
    filled_north, north_flags = _fill_scalars(north, max_gap)
    # Both components share the same gaps, so the flags match. Require both.
    flags = [left and right for left, right in zip(interpolated, north_flags, strict=True)]
    for index, _flag in enumerate(flags):
        if interpolated[index] != north_flags[index]:
            filled_east[index] = None
            filled_north[index] = None
            flags[index] = False
    return filled_east, filled_north, flags


def _fill_scalars(
    values: list[float | None],
    max_gap: int,
) -> tuple[list[float | None], list[bool]]:
    filled = list(values)
    interpolated = [False] * len(values)
    index = 0
    while index < len(values):
        if values[index] is not None:
            index += 1
            continue
        start = index
        while index < len(values) and values[index] is None:
            index += 1
        gap = index - start
        left = start - 1
        right = index
        if (
            gap <= max_gap
            and left >= 0
            and right < len(values)
            and values[left] is not None
            and values[right] is not None
        ):
            left_value = values[left]
            right_value = values[right]
            assert left_value is not None and right_value is not None
            for step in range(gap):
                fraction = (step + 1) / (gap + 1)
                filled[start + step] = left_value + (right_value - left_value) * fraction
                interpolated[start + step] = True
    return filled, interpolated


def _from_components(u_east: float | None, v_north: float | None) -> tuple[float, float] | None:
    if u_east is None or v_north is None:
        return None
    return speed_and_direction(u_east, v_north)


def _mean_vector(samples: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if not samples:
        return None, None
    east = []
    north = []
    speeds = []
    for speed, direction in samples:
        u_east, v_north = wind_components(speed, direction)
        east.append(u_east)
        north.append(v_north)
        speeds.append(speed)
    _speed, direction = speed_and_direction(sum(east) / len(east), sum(north) / len(north))
    return sum(speeds) / len(speeds), direction
