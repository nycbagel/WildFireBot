"""Command line forecast. The default is live data; fixtures are an explicit switch."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from wildfirebot import __version__
from wildfirebot.fires.cluster import Cluster
from wildfirebot.geo import compass_point, haversine_km, initial_bearing
from wildfirebot.pipeline import ForecastReport, HourScore, build_report
from wildfirebot.sources.firms import DEFAULT_SOURCES, parse_sources
from wildfirebot.tools import build_runtime


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != "forecast":
        parser.print_help()
        return 2
    try:
        _validate(args)
        now = _parse_time(args.now) if args.now else datetime.now(UTC)
        load_dotenv(Path(args.env_file))
        runtime = build_runtime(
            sources=parse_sources(args.sources),
            cluster_km=args.cluster_km,
            min_confidence=args.min_confidence,
            refresh=args.refresh,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            fixture_dir=Path(args.fixture_dir) if args.fixture_dir else None,
            now=now,
        )
        report = build_report(
            latitude=args.lat,
            longitude=args.lon,
            hours=args.hours,
            radius_km=args.radius_km,
            detection_days=args.days,
            cluster_km=args.cluster_km,
            lookback_hours=args.lookback_hours,
            now=now,
            firms=runtime.firms,
            weather=runtime.weather,
            airnow=runtime.airnow,
            openaq=runtime.openaq,
            data_mode=runtime.data_mode,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def format_report(report: ForecastReport) -> str:
    lines = [
        "WildFireBot smoke forecast",
        f"Target: {report.latitude:.3f}, {report.longitude:.3f}",
        f"Window: {report.hours} hours from {report.generated_at:%Y-%m-%d %H:%MZ}",
        f"Search radius: {report.radius_km:.0f} km",
        f"DATA MODE: {report.data_mode}",
        "",
        _plain_summary(report),
        "",
        _format_observations(report),
        "",
        _format_clusters(report),
        "",
        _format_wind(report),
        "",
        _format_table(report),
    ]
    if report.notices:
        lines.append("")
        lines.append("DEGRADED MODE" if report.degraded else "Data notes")
        lines.extend(f"- {note}" for note in report.notices)
    lines.extend(["", report.disclaimer])
    return "\n".join(lines)


def load_dotenv(path: Path) -> None:
    """Fill missing environment variables from a local .env file. Existing values win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aqi-smoke",
        description=(
            "Estimate relative wildfire smoke exposure at a point from NASA FIRMS detections "
            "and an Open-Meteo wind forecast. The default is live data."
        ),
    )
    parser.add_argument("--version", action="version", version=f"wildfirebot {__version__}")
    sub = parser.add_subparsers(dest="command")
    forecast = sub.add_parser("forecast", help="Print clusters, wind, and an hourly smoke score")
    forecast.add_argument("--lat", type=float, required=True, help="Target latitude")
    forecast.add_argument("--lon", type=float, required=True, help="Target longitude")
    forecast.add_argument("--hours", type=int, default=48, help="Forecast length (default 48)")
    forecast.add_argument(
        "--radius-km", type=float, default=250, help="Fire search radius (default 250)"
    )
    forecast.add_argument(
        "--days", type=int, default=2, help="Days of FIRMS detections (default 2)"
    )
    forecast.add_argument("--cluster-km", type=float, default=3, help="Hotspot clustering radius")
    forecast.add_argument("--lookback-hours", type=int, default=12, help="Hours of back-trajectory")
    forecast.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="Comma-separated FIRMS sources in priority order",
    )
    forecast.add_argument(
        "--min-confidence", type=float, default=30, help="Minimum numeric MODIS confidence"
    )
    forecast.add_argument("--cache-dir", default=None, help="Response cache directory")
    forecast.add_argument(
        "--refresh", action="store_true", help="Ignore fresh cache and request live data"
    )
    forecast.add_argument("--env-file", default=".env", help="Optional dotenv path")
    forecast.add_argument(
        "--now",
        default=None,
        help="ISO-8601 clock override. The default is the current UTC time.",
    )
    forecast.add_argument(
        "--fixture-dir",
        default=None,
        help=(
            "Developer and test switch. Read recorded API responses from this directory "
            "instead of live feeds. Omit it to use live APIs."
        ),
    )
    return parser


def _validate(args: argparse.Namespace) -> None:
    if not -90 <= args.lat <= 90 or not -180 <= args.lon <= 180:
        raise ValueError("latitude must be between -90 and 90 and longitude between -180 and 180")
    if not 1 <= args.hours <= 120:
        raise ValueError("--hours must be between 1 and 120")
    if not 1 <= args.radius_km <= 2000:
        raise ValueError("--radius-km must be between 1 and 2000")
    if not 1 <= args.days <= 10:
        raise ValueError("--days must be between 1 and 10")
    if not 0.5 <= args.cluster_km <= 50:
        raise ValueError("--cluster-km must be between 0.5 and 50")
    if not 1 <= args.lookback_hours <= 48:
        raise ValueError("--lookback-hours must be between 1 and 48")
    if not 0 <= args.min_confidence <= 100:
        raise ValueError("--min-confidence must be between 0 and 100")
    parse_sources(args.sources)


def _parse_time(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"could not parse --now {value!r}; use ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _plain_summary(report: ForecastReport) -> str:
    scored = [hour for hour in report.hourly if hour.score is not None]
    if not scored:
        return (
            "A smoke-exposure score could not be computed for this window. "
            "The notes below say which feed failed."
        )
    peak = max(scored, key=lambda hour: hour.score if hour.score is not None else -1)
    assert peak.score is not None
    when = f"{peak.time:%Y-%m-%d %H:%MZ}"
    if peak.score < 0.3:
        text = (
            f"Over the next {report.hours} hours, no detected fire lines up strongly with the "
            f"forecast wind. The peak relative score is {peak.score:.2f} at {when} ({peak.label})."
        )
    else:
        text = (
            f"Over the next {report.hours} hours, relative smoke exposure peaks at {when} "
            f"({peak.label}, score {peak.score:.2f})."
        )
    if peak.top_cluster is not None and 0 <= peak.top_cluster < len(report.clusters):
        cluster = report.clusters[peak.top_cluster]
        distance = haversine_km(
            report.latitude, report.longitude, cluster.latitude, cluster.longitude
        )
        bearing = initial_bearing(
            report.latitude, report.longitude, cluster.latitude, cluster.longitude
        )
        text += (
            f" Most of that peak comes from the cluster {distance:.0f} km {compass_point(bearing)} "
            f"({cluster.frp_mw:.0f} MW total FRP)."
        )
    return text


def _format_observations(report: ForecastReport) -> str:
    frame = report.observations
    if frame is None or frame.empty:
        return "Current PM2.5: no monitor reading in this window."
    lines = ["Current PM2.5 (nearest monitors)"]
    for row in frame.head(3).to_dict("records"):
        name = row.get("site_name") or "monitor"
        source = row.get("source") or "monitor"
        distance = row.get("distance_km")
        distance_text = f", {distance:.0f} km away" if isinstance(distance, (int, float)) else ""
        pm25 = row.get("pm25")
        aqi = row.get("aqi")
        category = row.get("category") or ""
        if pm25 is not None and pm25 == pm25 and aqi is not None:
            line = (
                f"- {name} ({source}): {float(pm25):.1f} µg/m³, breakpoint AQI {int(aqi)} "
                f"{category}{distance_text}"
            )
            reported = row.get("reported_aqi")
            if (
                reported is not None
                and reported == reported
                and abs(float(reported) - int(aqi)) > 5
            ):
                line += f" (the feed reported AQI {int(round(float(reported)))})"
        elif aqi is not None:
            line = (
                f"- {name} ({source}): reported AQI {int(aqi)} {category}; "
                f"concentration was not in the feed{distance_text}"
            )
        else:
            continue
        label = row.get("time_label")
        if label:
            line += f" at {label}"
        lines.append(line)
    if len(lines) == 1:
        return "Current PM2.5: no monitor reading in this window."
    lines.append(
        "Breakpoint AQI uses the EPA 2024 PM2.5 scale on the reported concentration, not a 24-hour average."
    )
    return "\n".join(lines)


def _format_clusters(report: ForecastReport) -> str:
    if not report.clusters:
        return f"Fire clusters: none within {report.radius_km:.0f} km after quality checks."
    lines = [f"Fire clusters ({len(report.clusters)}) within {report.radius_km:.0f} km"]
    ranked = sorted(
        report.clusters,
        key=lambda cluster: haversine_km(
            report.latitude, report.longitude, cluster.latitude, cluster.longitude
        ),
    )
    for index, cluster in enumerate(ranked, start=1):
        lines.append(f"{index}. {_cluster_line(report, cluster)}")
    return "\n".join(lines)


def _cluster_line(report: ForecastReport, cluster: Cluster) -> str:
    distance = haversine_km(report.latitude, report.longitude, cluster.latitude, cluster.longitude)
    bearing = initial_bearing(
        report.latitude, report.longitude, cluster.latitude, cluster.longitude
    )
    levels = ",".join(cluster.processing_levels) or "unknown"
    sources = ",".join(cluster.sources) or "unknown"
    frp = f"{cluster.frp_mw:.0f} MW total FRP" if cluster.frp_mw > 0 else "no FRP (detection only)"
    spots = "1 hotspot" if cluster.count == 1 else f"{cluster.count} hotspots"
    return (
        f"{cluster.latitude:.3f}, {cluster.longitude:.3f}  {distance:.0f} km {compass_point(bearing)}  "
        f"{frp}  {spots}  {sources} ({levels})  "
        f"last seen {cluster.last_seen:%Y-%m-%d %H:%MZ}"
    )


def _format_wind(report: ForecastReport) -> str:
    summary = report.wind_summary
    if summary is None:
        return "Wind summary: unavailable."
    lines = ["Wind summary"]
    lines.append(_wind_line("10 m", summary.speed_10m_mean, summary.direction_10m_prevailing))
    lines.append(_wind_line("850 hPa", summary.speed_850_mean, summary.direction_850_prevailing))
    if summary.blh_mean is None:
        lines.append("Boundary layer: unavailable")
    else:
        lines.append(
            f"Boundary layer: mean {summary.blh_mean:.0f} m "
            f"(min {summary.blh_min:.0f}, max {summary.blh_max:.0f})"
        )
    lines.append(
        f"Interpolated hours: {summary.interpolated_hours}. "
        f"Hours using 850 hPa in place of 10 m: {summary.substituted_hours}. "
        f"Hours still missing: {summary.missing_transport_hours}."
    )
    return "\n".join(lines)


def _wind_line(label: str, speed: float | None, direction: float | None) -> str:
    if speed is None or direction is None:
        return f"{label}: unavailable"
    return f"{label}: mean {speed:.1f} m/s, prevailing from {direction:.0f}° {compass_point(direction)}"


def _format_table(report: ForecastReport) -> str:
    lines = [
        "Hourly smoke-exposure score",
        "time (UTC)          score  label                      wind                         blh m  notes",
    ]
    for hour in report.hourly:
        lines.append(_hour_row(hour))
    lines.append(
        "Score is a relative exposure index (FRP, upwind distance, boundary-layer height). It is not an AQI."
    )
    return "\n".join(lines)


def _hour_row(hour: HourScore) -> str:
    score = f"{hour.score:6.2f}" if hour.score is not None else "   n/a"
    if hour.speed_m_s is None or hour.direction_deg is None:
        wind = "wind n/a                 "
    else:
        wind = f"{hour.speed_m_s:4.1f} m/s from {hour.direction_deg:5.0f}° {compass_point(hour.direction_deg):<3}"
    blh = f"{hour.blh_m:6.0f}" if hour.blh_m is not None else "   n/a"
    notes = ", ".join(hour.notes)
    return f"{hour.time:%Y-%m-%d %H:%MZ}  {score}  {hour.label:<26}  {wind:<28}  {blh}  {notes}"
