"""End-to-end outlook with a fake transport. No live API calls."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from wildfirebot.config import Settings
from wildfirebot.forecast import build_forecast, main
from wildfirebot.geo import bearing_deg, haversine_km
from wildfirebot.plume import SmokeSource, source_increment_ug_m3

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
SETTINGS = Settings(
    firms_map_key="MAP-SECRET",
    airnow_api_key="AIRNOW-SECRET",
    nws_user_agent="WildFireBot (contact@example.com)",
)
TARGET = (37.0, -122.0)


def _hourly_forecast() -> str:
    periods = []
    for hour in range(0, 56):
        start = NOW + timedelta(hours=hour)
        end = start + timedelta(hours=1)
        speed = "10 mph" if hour < 6 else "20 mph"
        periods.append(
            {
                "startTime": start.isoformat(),
                "endTime": end.isoformat(),
                "windSpeed": speed,
                "windDirection": "W",
            }
        )
    return json.dumps({"properties": {"periods": periods}})


def _open_meteo() -> str:
    times = []
    values = []
    for hour in range(0, 60):
        stamp = NOW + timedelta(hours=hour)
        times.append(stamp.strftime("%Y-%m-%dT%H:%M"))
        values.append(5.0 if hour == 0 else 6.5)
    return json.dumps({"hourly": {"time": times, "pm2_5": values}})


def _firms_csv() -> str:
    # One fire due west (downwind of a west wind) and one due east (upwind).
    # The upwind fire is much stronger, so a missed downwind test would show it.
    return "\n".join(
        [
            "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight",
            "37.0,-123.0,340,0.4,0.4,2026-11-19,1800,N20,VIIRS,n,2.0NRT,300,80,D",
            "37.0,-121.0,360,0.4,0.4,2026-11-19,1800,N20,VIIRS,h,2.0NRT,310,500,D",
            "0.0,0.0,300,0.4,0.4,2026-11-19,1800,N20,VIIRS,l,2.0NRT,290,10,D",
        ]
    )


def _airnow_observation() -> str:
    return json.dumps(
        [
            {
                "reportingAreaName": "Oakland",
                "parameterName": "PM2.5",
                "concentration": 8.4,
                "nowcastAQI": 46,
                "aqiCategoryName": "Good",
            }
        ]
    )


def _airnow_forecast() -> str:
    return json.dumps(
        [
            {
                "dateForecast": "2026-11-19",
                "reportingArea": "Oakland",
                "parameterName": "PM2.5",
                "aqi": 55,
                "aqiCategoryName": "Moderate",
            }
        ]
    )


class Router:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        self.calls.append(url)
        if "firms.modaps.eosdis.nasa.gov" in url:
            return 200, _firms_csv()
        if "api.weather.gov/points" in url:
            return 200, json.dumps(
                {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/MTR/1,2/forecast/hourly"}}
            )
        if "api.weather.gov/gridpoints" in url:
            return 200, _hourly_forecast()
        if "observation/current/ziplatlong" in url:
            return 200, _airnow_observation()
        if "aq/forecast/current" in url:
            return 200, _airnow_forecast()
        if "air-quality-api.open-meteo.com" in url:
            return 200, _open_meteo()
        raise AssertionError(url)


def test_outlook_adds_downwind_smoke_only_after_it_arrives():
    router = Router()
    result = build_forecast(
        TARGET[0],
        TARGET[1],
        radius_km=150,
        settings=SETTINGS,
        transport=router,
        now=NOW,
    )
    encoded = json.dumps(result)
    assert "MAP-SECRET" not in encoded
    assert "AIRNOW-SECRET" not in encoded
    assert "/aq/observation/latLong/current/" not in "".join(router.calls)

    assert result["fire_count"] == 2
    fires = result["fires"]
    assert isinstance(fires, list)
    west = next(fire for fire in fires if fire["longitude"] == -123.0)
    east = next(fire for fire in fires if fire["longitude"] == -121.0)
    assert west["direction"] == "W"
    assert west["bearing_deg"] == pytest.approx(90.0, abs=0.5)
    assert west["downwind"] is True
    assert west["confidence"] == "n"
    assert west["frp_mw"] == 80
    assert west["acq_date"] == "2026-11-19"
    assert west["acq_time"] == "1800"
    assert east["downwind"] is False
    assert east["frp_mw"] == 500

    horizons = {item["hours"]: item for item in result["horizons"]}
    assert list(horizons) == [0, 6, 12, 24, 48]
    assert horizons[0]["smoke_pm25_ug_m3"] == 0.0
    assert horizons[0]["baseline_pm25_ug_m3"] == 8.4
    assert horizons[0]["baseline_source"] == "AirNow"
    assert horizons[0]["total_pm25_ug_m3"] == 8.4
    assert horizons[0]["aqi"] == 47
    assert horizons[0]["category"] == "Good"
    assert horizons[0]["arrived_fire_count"] == 0

    distance_m = haversine_km(37.0, -122.0, 37.0, -123.0) * 1000.0
    bearing = bearing_deg(37.0, -123.0, 37.0, -122.0)
    expected = source_increment_ug_m3(
        SmokeSource(distance_m, bearing, 80.0),
        wind_from_deg=270.0,
        wind_speed_m_s=20 * 0.44704,
        horizon_hours=6,
    )
    assert horizons[6]["smoke_pm25_ug_m3"] == pytest.approx(round(expected, 2))
    assert horizons[6]["smoke_pm25_ug_m3"] > 0
    assert horizons[6]["baseline_source"] == "Open-Meteo"
    assert horizons[6]["baseline_pm25_ug_m3"] == 6.5
    assert horizons[6]["total_pm25_ug_m3"] == pytest.approx(
        round(6.5 + expected, 2)
    )
    assert horizons[6]["arrived_fire_count"] == 1
    assert horizons[6]["contributors"][0]["longitude"] == -123.0
    assert horizons[12]["smoke_pm25_ug_m3"] == horizons[6]["smoke_pm25_ug_m3"]
    assert horizons[6]["aqi"] is not None
    assert horizons[6]["health_message"]

    notes = " ".join(result["notes"])
    assert "AirNow's own forecast" in notes
    assert "AQI 55" in notes


def test_missing_keys_skip_those_sources_and_keep_the_others():
    router = Router()
    result = build_forecast(
        TARGET[0],
        TARGET[1],
        radius_km=150,
        settings=Settings(firms_map_key="", airnow_api_key="", nws_user_agent="WildFireBot (contact@example.com)"),
        transport=router,
        now=NOW,
    )
    assert result["sources"]["firms"]["status"] == "skipped"
    assert result["sources"]["airnow"]["status"] == "skipped"
    assert result["sources"]["nws"]["status"] == "ok"
    assert result["sources"]["open_meteo"]["status"] == "ok"
    assert result["fire_count"] == 0
    assert result["horizons"][0]["baseline_source"] == "Open-Meteo"
    assert result["horizons"][0]["baseline_pm25_ug_m3"] == 5.0
    joined = " ".join(router.calls)
    assert "firms.modaps.eosdis.nasa.gov" not in joined
    assert "airnowapi.org" not in joined
    summary_notes = " ".join(result["notes"])
    assert "FIRMS_MAP_KEY" in summary_notes
    assert "AIRNOW_API_KEY" in summary_notes


def test_cli_prints_a_summary_and_json(capsys):
    router = Router()
    code = main(
        ["--lat", "37", "--lon", "-122", "--radius-km", "150"],
        settings=SETTINGS,
        transport=router,
        now=NOW,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "WildFireBot smoke outlook" in out
    assert "Downwind of the current wind: yes" in out
    assert "AQI" in out
    payload = json.loads(out.split("--- json ---", 1)[1])
    assert payload["location"]["latitude"] == 37
    assert payload["horizons"][0]["hours"] == 0


def test_cli_json_only_is_a_single_document(capsys):
    code = main(
        ["--lat", "37", "--lon", "-122", "--json-only"],
        settings=SETTINGS,
        transport=Router(),
        now=NOW,
    )
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["fire_count"] == 2
    assert "WildFireBot smoke outlook" not in out


def test_cli_rejects_a_bad_latitude():
    code = main(
        ["--lat", "120", "--lon", "0"],
        settings=SETTINGS,
        transport=lambda url, headers=None: (_ for _ in ()).throw(AssertionError("network")),
    )
    assert code == 2


def test_cli_requires_coordinates():
    with pytest.raises(SystemExit) as caught:
        main([])
    assert caught.value.code == 2
