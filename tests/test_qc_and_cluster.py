from datetime import UTC, datetime

import pandas as pd

from wildfirebot.fires.cluster import cluster_hotspots
from wildfirebot.fires.qc import filter_detections

WHEN = datetime(2026, 11, 19, 18, tzinfo=UTC)


def _row(**overrides):
    row = {
        "latitude": 38.0,
        "longitude": -122.0,
        "acquired_at": WHEN,
        "frp": 20.0,
        "confidence": "h",
        "hotspot_type": None,
    }
    row.update(overrides)
    return row


def test_filters_low_confidence_invalid_frp_and_static_sources():
    rows = [
        _row(),
        _row(confidence="l"),
        _row(confidence=15),
        _row(confidence=80),
        _row(latitude=91),
        _row(frp=-5),
        _row(frp="nope"),
        _row(hotspot_type=2),
        _row(confidence="M"),
    ]
    kept, reasons = filter_detections(rows, min_modis_confidence=30)
    assert len(kept) == 3
    assert reasons["low_confidence"] == 2
    assert reasons["invalid_coordinate"] == 1
    assert reasons["invalid_frp"] == 2
    assert reasons["non_wildfire"] == 1


def test_cluster_centroid_is_weighted_by_frp():
    frame = pd.DataFrame(
        [
            _point(38.0, -122.0, 10),
            _point(38.01, -122.0, 30),
            _point(38.5, -122.0, 100),
        ]
    )
    clusters = cluster_hotspots(frame, radius_km=5)
    assert len(clusters) == 2
    paired = next(cluster for cluster in clusters if cluster.count == 2)
    assert paired.frp_mw == pytest_approx_40()
    assert paired.latitude == pytest_approx(38.0075)
    assert paired.longitude == pytest_approx(-122.0)


def test_landsat_cluster_without_frp_stays_at_zero():
    frame = pd.DataFrame([_point(38.0, -122.0, None), _point(38.01, -122.0, None)])
    clusters = cluster_hotspots(frame, radius_km=5)
    assert len(clusters) == 1
    assert clusters[0].frp_mw == 0
    assert clusters[0].count == 2


def _point(lat, lon, frp):
    return {
        "latitude": lat,
        "longitude": lon,
        "frp": frp,
        "acquired_at": WHEN,
        "source": "VIIRS_NOAA21_NRT",
        "processing_level": "NRT",
        "satellite": "N21",
    }


def pytest_approx(value):
    import pytest

    return pytest.approx(value)


def pytest_approx_40():
    import pytest

    return pytest.approx(40.0)
