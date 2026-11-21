"""Place name to coordinates with Open-Meteo's keyless geocoder."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

from wildfirebot.http import HttpClient, HttpError
from wildfirebot.xbot.parse import search_names

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
PARK_CODES = frozenset({"PRK", "PRKS", "RSRV", "FRST", "MT", "MTS", "PK", "LK", "ISL"})
SEAT_CODES = frozenset({"PPLC", "PPLA", "PPLA2"})


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    region: str

    @property
    def label(self) -> str:
        return self.name


def _score(result: dict, wanted: str, position: int) -> tuple:
    code = result.get("feature_code")
    named = str(result.get("name", "")).lower().startswith(wanted.lower())
    tier = 3 if code in PARK_CODES and named else 2 if code in SEAT_CODES else 1 if named else 0
    return (tier, result.get("population") or 0, -position)


def _search(http: HttpClient, name: str) -> list[dict]:
    url = f"{GEOCODE_URL}?{urlencode({'name': name, 'count': 10, 'language': 'en', 'format': 'json'})}"
    try:
        payload = http.get_text(
            url, ttl=timedelta(days=30), accept=lambda text: isinstance(json.loads(text), dict)
        )
        return list(json.loads(payload.text).get("results") or [])
    except (HttpError, ValueError):
        return []


def geocode(http: HttpClient, candidate: str) -> Place | None:
    """Best match for a name. A park beats a village of the same name; else the bigger place."""
    for name in search_names(candidate):
        results: list[dict] = []
        for query in (f"{name} National Park", name):
            results.extend(r for r in _search(http, query) if r not in results)
        results = [r for r in results if isinstance(r.get("latitude"), (int, float))]
        if not results:
            continue
        ranked = sorted(
            enumerate(results), key=lambda item: _score(item[1], name, item[0]), reverse=True
        )
        best = ranked[0][1]
        region = ", ".join(str(best[k]) for k in ("admin1", "country_code") if best.get(k))
        return Place(str(best["name"]), float(best["latitude"]), float(best["longitude"]), region)
    return None
