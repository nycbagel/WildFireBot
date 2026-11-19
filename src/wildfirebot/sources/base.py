"""Shared fetch contract for fire, weather, and observation clients."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pandas as pd

from wildfirebot.geo import BBox

__all__ = [
    "BBox",
    "DataSource",
    "SourceResult",
    "TimeRange",
    "floor_hour",
    "optional_float",
]


@dataclass(frozen=True)
class TimeRange:
    """Inclusive, timezone-aware interval."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("time range start and end must be timezone-aware")
        if self.end < self.start:
            raise ValueError("time range end is before start")


@dataclass
class SourceResult:
    """One fetch.

    `data` is the table contributors should fill. The other fields tell the forecast
    whether this feed is missing, stale, or only partly available so the CLI can say so
    instead of presenting a partial answer as complete.
    """

    data: pd.DataFrame
    notices: list[str] = field(default_factory=list)
    degraded: bool = False
    stale: bool = False
    sources_used: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)


class DataSource(Protocol):
    """Every feed implements this one method: box plus time range, then a table."""

    name: str

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        """Fetch observations that fall inside `bbox` and `time_range`."""


def floor_hour(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    return value.replace(minute=0, second=0, microsecond=0)


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def age_of(stored_at: datetime | None, *, now: datetime | None = None) -> timedelta | None:
    if stored_at is None:
        return None
    current = now or datetime.now(UTC)
    return current - stored_at
