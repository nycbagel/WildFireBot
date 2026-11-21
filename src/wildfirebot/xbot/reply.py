"""Danger or all-clear decision and the reply text (under 280 characters, no links)."""

from __future__ import annotations

from dataclasses import dataclass

from wildfirebot.model.plume import STABLE_AIR_NOTE

MAX_CHARS = 280
SAFE_CHARS = 278  # X counts plain text one per character; no links are ever added
FORECAST_REACH_H = 48
CAVEAT = "Screening estimate, not a health advisory. Check AirNow and local officials."
ASK_PLACE = (
    "Which place? Reply with a place name (like Yosemite) and optional dates, and I'll check "
    "fire and smoke risk. Screening estimate, not a health advisory."
)


@dataclass(frozen=True)
class Thresholds:
    aqi: int = 101
    fire_km: float = 50.0
    min_fire_frp_mw: float = 10.0


@dataclass(frozen=True)
class Decision:
    kind: str  # "danger" | "all_clear"
    worst_aqi: int | None
    worst_category: str | None
    worst_hour: int | None
    aqi_danger: bool
    fire: dict | None  # nearest qualifying fire cluster
    fire_danger: bool
    beyond_forecast: bool
    stable_air: bool
    limited_data: bool
    fire_feed_ok: bool


def window_horizons(start_h: float, end_h: float) -> list[int]:
    """3-hourly forecast hours that cover [start_h, end_h]; empty past the 48 h reach."""
    if start_h > FORECAST_REACH_H:
        return []
    end = min(end_h, FORECAST_REACH_H)
    first = max(0, int(start_h // 3) * 3)
    last = min(FORECAST_REACH_H, int(-(-end // 3)) * 3)
    return list(range(first, max(last, first) + 1, 3))


def decide(forecast: dict, start_h: float, end_h: float, limits: Thresholds) -> Decision:
    """Danger when forecast AQI in the window reaches the threshold or a real fire is close."""
    beyond = start_h > FORECAST_REACH_H
    rows = [
        row
        for row in forecast.get("horizons", [])
        if not beyond and start_h - 3 < row["horizon_hours"] < end_h + 3
    ]
    worst = max(rows, key=lambda r: r["aqi"]) if rows else None
    fires = [
        c for c in forecast.get("clusters", []) if (c.get("frp_mw") or 0) >= limits.min_fire_frp_mw
    ]
    fire = fires[0] if fires else None  # clusters arrive sorted by distance
    aqi_danger = worst is not None and worst["aqi"] >= limits.aqi
    fire_danger = fire is not None and fire["distance_km"] <= limits.fire_km
    notes = [note for row in rows for note in row.get("notes", [])]
    return Decision(
        kind="danger" if aqi_danger or fire_danger else "all_clear",
        worst_aqi=None if worst is None else worst["aqi"],
        worst_category=None if worst is None else worst["category"],
        worst_hour=None if worst is None else worst["horizon_hours"],
        aqi_danger=aqi_danger,
        fire=fire,
        fire_danger=fire_danger,
        beyond_forecast=beyond,
        stable_air=STABLE_AIR_NOTE in notes,
        limited_data=any("wind unavailable" in n for n in notes),
        fire_feed_ok=any(
            str(n).startswith(("FIRMS detections from", "FIRMS returned no detections"))
            for n in forecast.get("notices", [])
        ),
    )


def _fire_text(fire: dict) -> str:
    direction = fire.get("direction_from_target")
    where = f"{fire['distance_km']:.0f} km" + (f" {direction}" if direction else "")
    return f"nearest active fire ~{where} ({fire['frp_mw']:.0f} MW)"


def compose(place: str, when_label: str, decision: Decision, limits: Thresholds) -> str:
    """Reply text. Optional parts are dropped until it fits; the caveat always stays."""
    place = place if len(place) <= 40 else place[:39] + "…"
    required: list[str] = []
    optional: list[str] = []
    if decision.kind == "danger":
        head = f"Heads-up for {place} ({when_label}):"
        if decision.aqi_danger:
            required.append(
                f"forecast AQI up to {decision.worst_aqi} ({decision.worst_category})"
                + (f" near +{decision.worst_hour}h" if decision.worst_hour else "")
            )
        if decision.fire is not None and (decision.fire_danger or not decision.aqi_danger):
            required.append(_fire_text(decision.fire))
        elif decision.fire is not None:
            optional.append(_fire_text(decision.fire))
        body = "; ".join(required) + "."
    else:
        head = f"{place} ({when_label}):"
        if decision.beyond_forecast:
            body = "my forecast only reaches 48 h ahead, so this is today's fire picture only."
        elif decision.worst_aqi is not None:
            body = f"worst forecast AQI {decision.worst_aqi} ({decision.worst_category})"
            body += f", no active fire within {limits.fire_km:.0f} km."
        else:
            body = f"no active fire within {limits.fire_km:.0f} km; no AQI forecast available."
        if decision.beyond_forecast:
            body += (
                f" {_fire_text(decision.fire)}."
                if decision.fire is not None
                else f" No active fire within {limits.fire_km:.0f} km."
            )
        elif decision.fire is not None:
            fire_text = _fire_text(decision.fire)
            optional.append(f"{fire_text[0].upper()}{fire_text[1:]}.")
    extras = []
    if decision.stable_air:
        extras.append("Stable air: smoke may be underestimated.")
    if decision.limited_data:
        extras.append("Some data was unavailable.")
    if decision.beyond_forecast and decision.kind == "danger":
        extras.append("Forecast reaches only 48 h; check closer to your trip.")
    elif decision.kind == "all_clear" and decision.beyond_forecast:
        extras.append("Check again closer to your trip.")
    caveat = (
        CAVEAT
        if decision.kind == "danger"
        else "Not a guarantee or health advisory. Check AirNow and local officials."
    )
    parts = [head, body, *extras, *optional]
    while True:
        text = " ".join(parts + [caveat])
        if len(text) <= SAFE_CHARS or len(parts) <= 2:
            break
        parts.pop()  # drop the least important optional part
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1] + "…"
    return text
