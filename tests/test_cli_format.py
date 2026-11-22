from wildfirebot.cli import format_aqi


def _result():
    return {
        "latitude": 37.75,
        "longitude": -119.59,
        "radius_km": 150.0,
        "data_mode": "live",
        "fire_clusters": 0,
        "clusters": [],
        "horizons": [
            {
                "horizon_hours": hours,
                "time": time,
                "aqi": aqi,
                "category": category,
                "pm25": pm25,
                "baseline_pm25": pm25,
                "fire_pm25": 0.0,
            }
            for hours, time, aqi, category, pm25 in (
                (0, "2026-11-21T12:00:00Z", 57, "Moderate", 12.3),
                (12, "2026-11-22T00:00:00Z", 136, "Unhealthy for Sensitive Groups", 49.6),
            )
        ],
        "terrain_notes": [],
        "notices": [],
        "degraded": False,
        "disclaimer": "Screening estimate.",
    }


def test_aqi_table_header_lines_up_with_its_rows():
    lines = format_aqi(_result()).splitlines()
    header = next(line for line in lines if line.startswith("+h"))
    rows = [line for line in lines if line.startswith("+0 ") or line.startswith("+12 ")]
    assert len(rows) == 2
    for row in rows:
        # the time starts under "time (UTC)", the AQI's last digit under the "I" of "AQI",
        # and the category and PM2.5 values start under their headings
        assert row.index("2026-") == header.index("time (UTC)")
        assert row.index("  M" if "Moderate" in row else "  U") + 2 == header.index("category")
        aqi_end = header.index("AQI") + len("AQI")
        assert row[aqi_end - 1].isdigit() and row[aqi_end] == " "
        pm25_start = header.index("PM2.5")
        assert row[pm25_start : pm25_start + 6].strip() in ("12.3", "49.6")
