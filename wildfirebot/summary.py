"""Plain-language rendering of a forecast payload."""

from __future__ import annotations


def _plain_number(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _hours_phrase(hours: int) -> str:
    if hours == 0:
        return "Now"
    if hours == 1:
        return "In 1 hour"
    return f"In {hours} hours"


def _duration(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 1:
        minutes = max(1, int(round(hours * 60)))
        unit = "minute" if minutes == 1 else "minutes"
        return f"about {minutes} {unit}"
    return f"about {hours:.1f} hours"


def _fire_line(index: int, fire: dict[str, object]) -> str:
    distance = float(fire["distance_km"])
    direction = fire["direction"]
    bearing_deg = float(fire["bearing_deg"])
    frp = float(fire["frp_mw"])
    confidence = fire["confidence"] or "n/a"
    acquired = f"{fire['acq_date']} {fire['acq_time_display']} UTC".strip()
    downwind = fire["downwind"]
    arrival = fire["arrival_hours"]
    arrival_hours = float(arrival) if isinstance(arrival, (int, float)) else None
    if downwind is True:
        wind_text = f"yes. Arrival {_duration(arrival_hours)}"
    elif downwind is False:
        wind_text = "no"
    else:
        wind_text = "unknown (no wind forecast)"
    return (
        f"  {index}. {distance:.1f} km {direction} of this location "
        f"(bearing from the fire {bearing_deg:.0f}°), "
        f"FRP {frp:.1f} MW, confidence {confidence}, acquired {acquired}. "
        f"Downwind of the current wind: {wind_text}."
    )


def _horizon_line(horizon: dict[str, object]) -> str:
    label = _hours_phrase(int(horizon["hours"]))
    aqi = horizon.get("aqi")
    category = horizon.get("category")
    message = horizon.get("health_message")
    baseline = horizon.get("baseline_pm25_ug_m3")
    smoke = horizon.get("smoke_pm25_ug_m3")
    total = horizon.get("total_pm25_ug_m3")
    baseline_source = horizon.get("baseline_source") or "no baseline"
    arrived = horizon.get("arrived_fire_count")
    if aqi is None:
        missing = []
        if baseline is None:
            missing.append("no PM2.5 baseline")
        if smoke is None:
            missing.append("no smoke estimate")
        reason = " and ".join(missing) or "the total could not be computed"
        smoke_text = "unknown" if smoke is None else f"{smoke:.1f} µg/m³"
        return f"  {label}: no AQI ({reason}). Smoke increment {smoke_text}."
    health = f" {message}" if message else ""
    smoke_value = 0.0 if smoke is None else float(smoke)
    arrived_text = ""
    if arrived:
        count = int(arrived)
        noun = "fire" if count == 1 else "fires"
        arrived_text = f" {count} {noun} could have smoke here by then."
    return (
        f"  {label}: AQI {aqi} ({category}), {float(total):.1f} µg/m³ "
        f"(baseline {float(baseline):.1f} from {baseline_source}, "
        f"smoke {smoke_value:.1f}).{arrived_text}{health}"
    )


def render_summary(result: dict[str, object]) -> str:
    """Turn a ``build_forecast`` payload into a short readout."""

    location = result["location"]
    assert isinstance(location, dict)
    sources = result["sources"]
    assert isinstance(sources, dict)
    firms = sources.get("firms") or {}
    assert isinstance(firms, dict)
    model = result["model"]
    assert isinstance(model, dict)
    lines = [
        (
            f"WildFireBot smoke outlook for {float(location['latitude']):.4f}, "
            f"{float(location['longitude']):.4f}."
        ),
        (
            f"Search radius {float(location['radius_km']):g} km. "
            f"Fire source {firms.get('source', 'FIRMS')}."
        ),
        "",
        "Nearest fires (downwind and arrival use the current hour's wind):",
    ]
    fires = result.get("fires") or []
    assert isinstance(fires, list)
    if not fires:
        lines.append("  None inside the search radius.")
    else:
        for index, fire in enumerate(fires[:5], start=1):
            assert isinstance(fire, dict)
            lines.append(_fire_line(index, fire))
        hidden = int(result.get("fire_count") or 0) - min(5, len(fires))
        if hidden > 0:
            lines.append(f"  {hidden} more detections are included in the calculation and the JSON.")
    lines.append("")
    lines.append(
        "Outlook: baseline PM2.5 plus centerline smoke after the travel time has elapsed."
    )
    horizons = result.get("horizons") or []
    assert isinstance(horizons, list)
    for horizon in horizons:
        assert isinstance(horizon, dict)
        lines.append(_horizon_line(horizon))
    notes = result.get("notes") or []
    if notes:
        lines.append("")
        lines.append("Notes:")
        for note in notes:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append(
        "Model: steady Gaussian plume, rural stability "
        f"{model.get('stability_class')}, plume height {_plain_number(model.get('plume_height_m'))} m, "
        f"{_plain_number(model.get('emission_factor_kg_per_mj'))} kg PM2.5 per MJ of FRP. "
        f"Centerline value when this location is inside the "
        f"±{_plain_number(model.get('downwind_half_angle_deg'))}° downwind sector. "
        "Screening estimate only, not a regulatory forecast."
    )
    return "\n".join(lines).rstrip() + "\n"
