import pytest

from wildfirebot.geo import (
    bbox_from_point,
    compass_point,
    destination,
    haversine_km,
    initial_bearing,
)


def test_one_degree_of_latitude():
    distance = haversine_km(0, 0, 1, 0)
    assert distance == pytest.approx(111.195, abs=0.01)


def test_destination_north_and_back():
    lat, lon = destination(37.8, -122.3, bearing_deg=0, distance_m=50_000)
    assert lat > 37.8
    assert lon == pytest.approx(-122.3, abs=1e-6)
    assert haversine_km(37.8, -122.3, lat, lon) == pytest.approx(50.0, abs=0.05)
    bearing = initial_bearing(37.8, -122.3, lat, lon)
    assert min(bearing, 360 - bearing) == pytest.approx(0.0, abs=0.2)


def test_compass_points():
    assert compass_point(0) == "N"
    assert compass_point(45) == "NE"
    assert compass_point(180) == "S"
    assert compass_point(315) == "NW"


def test_antimeridian_box_is_rejected():
    with pytest.raises(ValueError, match="antimeridian"):
        bbox_from_point(37.8, 179.9, radius_km=100)
