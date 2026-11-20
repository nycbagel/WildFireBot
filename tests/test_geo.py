"""Distance, bearing, and the search box. No network."""

import math

import pytest

from wildfirebot.geo import (
    EARTH_RADIUS_KM,
    KM_PER_DEG_LAT,
    angular_separation_deg,
    bearing_deg,
    bounding_boxes,
    compass_point,
    format_box,
    haversine_km,
)


def test_same_point_is_zero_distance():
    assert haversine_km(37.87, -122.27, 37.87, -122.27) == pytest.approx(0.0, abs=1e-9)


def test_one_degree_of_longitude_on_the_equator():
    expected = EARTH_RADIUS_KM * math.radians(1.0)
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(expected, rel=1e-12)


def test_one_degree_of_latitude():
    expected = EARTH_RADIUS_KM * math.radians(1.0)
    assert haversine_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(expected, rel=1e-12)
    assert haversine_km(10.0, 20.0, 11.0, 20.0) == pytest.approx(expected, rel=1e-12)


def test_distance_is_symmetric():
    forward = haversine_km(37.77, -122.42, 34.05, -118.24)
    backward = haversine_km(34.05, -118.24, 37.77, -122.42)
    assert forward == pytest.approx(backward, rel=1e-12)
    assert 500 < forward < 700


@pytest.mark.parametrize(
    ("lat2", "lon2", "expected"),
    [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 90.0),
        (-1.0, 0.0, 180.0),
        (0.0, -1.0, 270.0),
    ],
)
def test_cardinal_bearings_from_the_origin(lat2, lon2, expected):
    assert bearing_deg(0.0, 0.0, lat2, lon2) == pytest.approx(expected, abs=1e-6)


def test_bearing_wraps_to_the_unit_circle():
    assert 0.0 <= bearing_deg(10.0, 10.0, -20.0, -30.0) < 360.0


def test_angular_separation_takes_the_short_way_around():
    assert angular_separation_deg(350.0, 10.0) == pytest.approx(20.0)
    assert angular_separation_deg(10.0, 350.0) == pytest.approx(20.0)
    assert angular_separation_deg(0.0, 180.0) == pytest.approx(180.0)
    assert angular_separation_deg(20.0, 20.0) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("degrees", "label"),
    [(0, "N"), (45, "NE"), (90, "E"), (180, "S"), (270, "W"), (359, "N")],
)
def test_compass_points(degrees, label):
    assert compass_point(degrees) == label


def test_bounding_box_covers_the_radius_and_contains_the_point():
    radius = KM_PER_DEG_LAT
    west, south, east, north = bounding_boxes(0.0, 0.0, radius)[0]
    assert south == pytest.approx(-1.0, abs=1e-6)
    assert north == pytest.approx(1.0, abs=1e-6)
    assert west == pytest.approx(-1.0, abs=1e-6)
    assert east == pytest.approx(1.0, abs=1e-6)
    assert west <= 0.0 <= east
    assert south <= 0.0 <= north
    assert format_box(west, south, east, north).count(",") == 3


def test_bounding_box_splits_at_the_antimeridian():
    boxes = bounding_boxes(0.0, 179.8, 50.0)
    assert len(boxes) == 2
    for west, south, east, north in boxes:
        assert west <= east
        assert south < 0.0 < north
        assert -180.0 <= west <= 180.0
        assert -180.0 <= east <= 180.0
