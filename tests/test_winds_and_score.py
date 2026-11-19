from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from wildfirebot.fires.cluster import Cluster
from wildfirebot.geo import destination
from wildfirebot.transport.score import (
    ScoreConfig,
    exposure_label,
    score_clusters,
    trajectory_points,
)
from wildfirebot.transport.winds import prepare_winds, wind_components

WHEN = datetime(2026, 11, 19, 20, tzinfo=UTC)
CONFIG = ScoreConfig()


def test_direction_interpolation_crosses_north_not_south():
    frame = pd.DataFrame(
        {
            "time": [WHEN, WHEN + timedelta(hours=1), WHEN + timedelta(hours=2)],
            "wind_speed_10m": [10.0, None, 10.0],
            "wind_direction_10m": [350.0, None, 10.0],
            "wind_speed_850hPa": [12.0, 12.0, 12.0],
            "wind_direction_850hPa": [350.0, 0.0, 10.0],
            "boundary_layer_height": [800.0, None, 800.0],
        }
    )
    hours = prepare_winds(frame, max_gap_hours=3)
    middle = hours[1]
    assert middle.interpolated_10m is True
    assert middle.direction_10m == pytest.approx(0.0, abs=1.0)
    assert middle.transport_source == "10m"
    assert middle.blh_m == pytest.approx(800.0)


def test_long_gap_stays_missing_and_bad_speed_is_not_used():
    times = [WHEN + timedelta(hours=i) for i in range(6)]
    speeds = [5.0, None, None, None, None, 5.0]
    directions = [90.0, None, None, None, None, 90.0]
    frame = pd.DataFrame(
        {
            "time": times,
            "wind_speed_10m": speeds,
            "wind_direction_10m": directions,
            "wind_speed_850hPa": [None] * 6,
            "wind_direction_850hPa": [None] * 6,
            "boundary_layer_height": [1000.0] * 6,
        }
    )
    hours = prepare_winds(frame, max_gap_hours=3)
    assert all(hour.speed_10m is None for hour in hours[1:5])
    assert hours[1].transport_source == "missing"

    negative = frame.copy()
    negative["wind_speed_10m"] = [5.0, -3.0, 5.0, 5.0, 5.0, 5.0]
    negative["wind_direction_10m"] = [90.0, 90.0, 90.0, 90.0, 90.0, 90.0]
    filled = prepare_winds(negative, max_gap_hours=3)
    assert filled[1].interpolated_10m is True
    assert filled[1].speed_10m == pytest.approx(5.0)
    assert filled[1].speed_10m > 0


def test_missing_surface_wind_uses_850_and_is_flagged():
    frame = pd.DataFrame(
        {
            "time": [WHEN],
            "wind_speed_10m": [None],
            "wind_direction_10m": [None],
            "wind_speed_850hPa": [8.0],
            "wind_direction_850hPa": [180.0],
            "boundary_layer_height": [-5.0],
        }
    )
    hour = prepare_winds(frame)[0]
    assert hour.transport_source == "850hPa"
    assert hour.transport_speed == pytest.approx(8.0)
    assert hour.transport_direction == pytest.approx(180.0)
    assert hour.blh_m is None


def test_component_sign_points_upwind_to_the_north():
    east, north = wind_components(10, 0)
    assert east == pytest.approx(0.0, abs=1e-9)
    assert north < 0
    target = (37.8, -122.3)
    points = trajectory_points(target[0], target[1], [(10.0, 0.0)])
    assert points[1][0] > target[0]


def test_upwind_fire_scores_and_downwind_fire_does_not():
    target = (37.8, -122.3)
    speed = 10.0
    upwind = destination(target[0], target[1], 0, speed * 3600)
    cluster = _cluster(upwind[0], upwind[1], 100)
    toward = trajectory_points(target[0], target[1], [(speed, 0.0)])
    away = trajectory_points(target[0], target[1], [(speed, 180.0)])
    score_up, parts, _note = score_clusters(target, toward, [cluster], WHEN, 800, CONFIG)
    score_down, _parts, _note = score_clusters(target, away, [cluster], WHEN, 800, CONFIG)
    assert score_up == pytest.approx(1.0, abs=0.03)
    assert parts[0][0] == 0
    assert score_down == 0
    assert exposure_label(score_up) == "light smoke possible"
    assert exposure_label(score_down) == "little expected smoke"


def test_shallow_boundary_layer_and_frp_scale_the_score():
    target = (0.0, 0.0)
    speed = 10.0
    point = destination(0, 0, 0, speed * 3600)
    cluster = _cluster(point[0], point[1], 100)
    doubled = _cluster(point[0], point[1], 200)
    path = trajectory_points(0, 0, [(speed, 0.0)])
    shallow, _, _ = score_clusters(target, path, [cluster], WHEN, 200, CONFIG)
    reference, _, _ = score_clusters(target, path, [cluster], WHEN, 800, CONFIG)
    capped, _, _ = score_clusters(target, path, [cluster], WHEN, 50, CONFIG)
    bigger, _, _ = score_clusters(target, path, [doubled], WHEN, 800, CONFIG)
    assert shallow == pytest.approx(reference * 4, rel=0.02)
    assert capped == pytest.approx(shallow, rel=0.02)
    assert bigger == pytest.approx(reference * 2, rel=0.02)


def test_local_fire_counts_even_when_the_wind_blows_away():
    target = (37.8, -122.3)
    nearby = destination(target[0], target[1], 180, 2_000)
    cluster = _cluster(nearby[0], nearby[1], 100)
    path = trajectory_points(target[0], target[1], [(10.0, 0.0)])
    score, _, _ = score_clusters(target, path, [cluster], WHEN, 800, CONFIG)
    assert score == pytest.approx(1.0, abs=0.03)


def _cluster(lat, lon, frp):
    return Cluster(
        latitude=lat,
        longitude=lon,
        frp_mw=frp,
        count=1,
        sources=("VIIRS_NOAA21_NRT",),
        processing_levels=("NRT",),
        first_seen=WHEN,
        last_seen=WHEN,
        satellites=("N21",),
    )
