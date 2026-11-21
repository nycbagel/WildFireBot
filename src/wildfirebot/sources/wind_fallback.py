"""Wind with a backup: Open-Meteo first, MET Norway only when Open-Meteo has nothing.

The hosted server shares its outbound address with other apps on the same host, so
Open-Meteo's free per-address limit can run out for reasons that have nothing to do
with WildFireBot. Every Open-Meteo request then comes back HTTP 429 and, without a
second source, every forecast loses its wind and with it the fires' smoke.
"""

from __future__ import annotations

from wildfirebot.geo import BBox
from wildfirebot.sources.base import DataSource, SourceResult, TimeRange
from wildfirebot.sources.openmeteo import WIND_OMITTED

# CC BY 4.0 asks for credit, a link to the licence, and a note of any changes
BACKUP_NOTE = (
    "Using MET Norway's forecast for the 10 m wind instead (data from MET Norway, "
    "CC BY 4.0, https://creativecommons.org/licenses/by/4.0/; WildFireBot uses only the "
    "10 m wind and fills short gaps). It has no 850 hPa wind or boundary-layer height, "
    "so hours that need them are flagged."
)


class FallbackWindClient:
    name = "wind"

    def __init__(self, primary: DataSource, backup: DataSource) -> None:
        self.primary = primary
        self.backup = backup

    def fetch(self, bbox: BBox, time_range: TimeRange) -> SourceResult:
        first = self.primary.fetch(bbox, time_range)
        if not first.data.empty:
            return first
        second = self.backup.fetch(bbox, time_range)
        if second.data.empty:
            notices = [*first.notices, *second.notices]
            if not any(WIND_OMITTED in note for note in notices):
                notices.append(WIND_OMITTED)
            return SourceResult(
                data=first.data,
                notices=notices,
                degraded=True,
                stale=first.stale or second.stale,
                sources_failed=[*first.sources_failed, *second.sources_failed],
            )
        # the main feed's own notice says smoke scores are omitted; with the backup they aren't
        reasons = [note.replace(WIND_OMITTED, "").strip() for note in first.notices]
        return SourceResult(
            data=second.data,
            notices=[*(note for note in reasons if note), BACKUP_NOTE, *second.notices],
            degraded=True,
            stale=second.stale,
            sources_used=list(second.sources_used),
            sources_failed=list(first.sources_failed),
        )
