"""Downwind rule and Gaussian plume sanity checks. No network."""

import math

import pytest

from wildfirebot.geo import bearing_deg, haversine_km
from wildfirebot.plume import (
    ModelConfig,
    SmokeSource,
    arrival_hours,
    dispersion_sigmas_m,
    ground_level_concentration_ug_m3,
    is_downwind,
    plume_increment_ug_m3,
    source_increment_ug_m3,
    wind_toward_deg,
)


def test_nws_wind_direction_is_where_it_comes_from():
    assert wind_toward_deg(270.0) == pytest.approx(90.0)
    assert wind_toward_deg(0.0) == pytest.approx(180.0)
    assert wind_toward_deg(180.0) == pytest.approx(0.0)


def test_fire_to_the_west_is_downwind_of_a_west_wind():
    """A west wind blows toward the east, onto a target that sits east of the fire.

    Forgetting the FROM-to-TOWARD flip makes this case look upwind.
    """

    target = (37.0, -122.0)
    fire = (37.0, -123.0)
    bearing = bearing_deg(fire[0], fire[1], target[0], target[1])
    # A parallel is not exactly due east on a sphere. 1° of longitude at 37°N
    # leaves the meridian by about 0.3°.
    assert bearing == pytest.approx(90.0, abs=0.5)
    assert is_downwind(bearing, wind_from_deg=270.0, half_angle_deg=30.0)
    assert not is_downwind(bearing, wind_from_deg=90.0, half_angle_deg=30.0)


def test_downwind_sector_includes_the_edge_and_wraps_past_north():
    assert is_downwind(90.0, wind_from_deg=270.0, half_angle_deg=30.0)
    assert is_downwind(120.0, wind_from_deg=270.0, half_angle_deg=30.0)
    assert not is_downwind(120.2, wind_from_deg=270.0, half_angle_deg=30.0)
    # Wind from 170° travels toward 350°. Bearing 10° is 20° off that heading.
    assert is_downwind(10.0, wind_from_deg=170.0, half_angle_deg=30.0)
    assert not is_downwind(40.0, wind_from_deg=170.0, half_angle_deg=30.0)


def test_arrival_time_is_distance_over_wind_speed():
    assert arrival_hours(3600.0, 1.0) == pytest.approx(1.0)
    assert arrival_hours(0.0, 5.0) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        arrival_hours(1000.0, 0.0)


def test_class_d_sigmas_match_the_briggs_formulas_at_1_km():
    sigma_y, sigma_z = dispersion_sigmas_m(1000.0, "D")
    assert sigma_y == pytest.approx(0.08 * 1000 * (1 + 0.0001 * 1000) ** -0.5)
    assert sigma_z == pytest.approx(0.06 * 1000 * (1 + 0.0015 * 1000) ** -0.5)


def test_sigmas_grow_with_distance_and_unstable_classes_spread_more():
    near_y, near_z = dispersion_sigmas_m(1000.0, "D")
    far_y, far_z = dispersion_sigmas_m(5000.0, "D")
    assert far_y > near_y
    assert far_z > near_z
    _, unstable = dispersion_sigmas_m(1000.0, "A")
    _, neutral = dispersion_sigmas_m(1000.0, "D")
    _, stable = dispersion_sigmas_m(1000.0, "F")
    assert unstable > neutral > stable


def test_ground_level_centerline_matches_the_closed_form():
    sigma_y, sigma_z = dispersion_sigmas_m(1000.0, "D")
    emission_ug_s = 1.0e9
    expected = emission_ug_s / (math.pi * 1.0 * sigma_y * sigma_z)
    got = ground_level_concentration_ug_m3(
        emission_kg_s=1.0,
        wind_speed_m_s=1.0,
        downwind_m=1000.0,
        crosswind_m=0.0,
        plume_height_m=0.0,
        stability="D",
    )
    assert got == pytest.approx(expected, rel=1e-12)


def test_elevated_plume_and_crosswind_offset_use_the_gaussian_terms():
    downwind_m = 2000.0
    crosswind_m = 100.0
    height_m = 200.0
    sigma_y, sigma_z = dispersion_sigmas_m(downwind_m, "D")
    emission_ug_s = 0.5e9
    expected = (
        emission_ug_s
        / (math.pi * 2.0 * sigma_y * sigma_z)
        * math.exp(-(crosswind_m**2) / (2.0 * sigma_y**2))
        * math.exp(-(height_m**2) / (2.0 * sigma_z**2))
    )
    got = ground_level_concentration_ug_m3(0.5, 2.0, downwind_m, crosswind_m, height_m, "D")
    assert got == pytest.approx(expected, rel=1e-12)


def test_concentration_falls_with_distance_and_rises_with_frp():
    def centerline(distance_m: float, emission_kg_s: float) -> float:
        return ground_level_concentration_ug_m3(
            emission_kg_s, 5.0, distance_m, 0.0, 0.0, "D"
        )

    near = centerline(1_000.0, 1.0)
    mid = centerline(5_000.0, 1.0)
    far = centerline(20_000.0, 1.0)
    assert near > mid > far > 0
    assert centerline(5_000.0, 2.0) == pytest.approx(mid * 2.0, rel=1e-12)


def test_concentration_is_symmetric_off_axis_and_lower_than_the_centerline():
    center = ground_level_concentration_ug_m3(1.0, 3.0, 5_000.0, 0.0, 0.0, "D")
    left = ground_level_concentration_ug_m3(1.0, 3.0, 5_000.0, 500.0, 0.0, "D")
    right = ground_level_concentration_ug_m3(1.0, 3.0, 5_000.0, -500.0, 0.0, "D")
    assert left == pytest.approx(right, rel=1e-12)
    assert left < center


def test_a_taller_plume_lowers_the_ground_level_concentration():
    low = ground_level_concentration_ug_m3(1.0, 4.0, 30_000.0, 0.0, 0.0, "D")
    high = ground_level_concentration_ug_m3(1.0, 4.0, 30_000.0, 0.0, 500.0, "D")
    assert high < low


def test_faster_wind_dilutes_the_plume():
    slow = ground_level_concentration_ug_m3(1.0, 2.0, 10_000.0, 0.0, 0.0, "D")
    fast = ground_level_concentration_ug_m3(1.0, 4.0, 10_000.0, 0.0, 0.0, "D")
    assert fast == pytest.approx(slow / 2.0, rel=1e-12)


def _west_of_target(distance_m: float, frp_mw: float) -> SmokeSource:
    return SmokeSource(distance_m=distance_m, bearing_deg=90.0, frp_mw=frp_mw)


def test_smoke_is_zero_until_arrival_and_zero_when_upwind():
    config = ModelConfig(plume_height_m=0.0)
    source = _west_of_target(distance_m=3600.0, frp_mw=50.0)
    # 3600 m at 1 m/s is exactly one hour. A west wind blows this plume east.
    assert source_increment_ug_m3(source, 270.0, 1.0, horizon_hours=0.5, config=config) == 0.0
    arrived = source_increment_ug_m3(source, 270.0, 1.0, horizon_hours=1.0, config=config)
    assert arrived > 0
    # An east wind blows the plume away from the target.
    assert source_increment_ug_m3(source, 90.0, 1.0, horizon_hours=10.0, config=config) == 0.0


def test_smoke_scales_with_frp_and_adds_across_fires():
    config = ModelConfig(plume_height_m=0.0, emission_factor_kg_per_mj=0.02)
    small = _west_of_target(10_000.0, 20.0)
    large = _west_of_target(10_000.0, 40.0)
    small_c = source_increment_ug_m3(small, 270.0, 5.0, 24.0, config)
    large_c = source_increment_ug_m3(large, 270.0, 5.0, 24.0, config)
    assert large_c == pytest.approx(small_c * 2.0, rel=1e-12)
    assert plume_increment_ug_m3([small, large], 270.0, 5.0, 24.0, config) == pytest.approx(
        small_c + large_c, rel=1e-12
    )


def test_real_distance_to_a_fire_due_west_feeds_the_same_bearing():
    distance_m = haversine_km(37.0, -122.0, 37.0, -123.0) * 1000.0
    bearing = bearing_deg(37.0, -123.0, 37.0, -122.0)
    source = SmokeSource(distance_m=distance_m, bearing_deg=bearing, frp_mw=80.0)
    early = source_increment_ug_m3(source, 270.0, 5.0, horizon_hours=0.0)
    later = source_increment_ug_m3(source, 270.0, 5.0, horizon_hours=48.0)
    assert early == 0.0
    assert later > 0.0
