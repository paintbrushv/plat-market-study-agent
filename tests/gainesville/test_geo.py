"""Unit tests for etl.gainesville.geo — polygon loader and point-in-polygon helper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from etl.gainesville.geo import load_polygon, point_in_polygon


def _write_geojson(path: Path, geometry: dict) -> None:
    """Write a minimal GeoJSON FeatureCollection with one feature."""
    path.write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": geometry}],
        }),
        encoding="utf-8",
    )


def test_load_polygon_missing_file_returns_none(tmp_path: Path) -> None:
    """load_polygon returns None when the file does not exist."""
    result = load_polygon(tmp_path / "nonexistent.geojson")
    assert result is None


def test_load_polygon_empty_features_returns_none(tmp_path: Path) -> None:
    """load_polygon returns None when the FeatureCollection has no features."""
    p = tmp_path / "empty.geojson"
    p.write_text(
        json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8"
    )
    # Clear the cache so the fresh file is read.
    load_polygon.cache_clear()
    result = load_polygon(p)
    assert result is None


def test_load_polygon_simple_polygon(tmp_path: Path) -> None:
    """load_polygon returns a valid shapely geometry for a simple Polygon."""
    p = tmp_path / "box.geojson"
    _write_geojson(
        p,
        {
            "type": "Polygon",
            "coordinates": [
                [[-97.18, 33.61], [-97.10, 33.61], [-97.10, 33.65], [-97.18, 33.65], [-97.18, 33.61]]
            ],
        },
    )
    load_polygon.cache_clear()
    poly = load_polygon(p)
    assert poly is not None
    assert poly.geom_type in ("Polygon", "MultiPolygon")


def test_load_polygon_multipolygon(tmp_path: Path) -> None:
    """load_polygon handles a MultiPolygon geometry without error."""
    p = tmp_path / "multi.geojson"
    ring1 = [[-97.18, 33.61], [-97.10, 33.61], [-97.10, 33.65], [-97.18, 33.65], [-97.18, 33.61]]
    ring2 = [[-97.05, 33.70], [-97.00, 33.70], [-97.00, 33.75], [-97.05, 33.75], [-97.05, 33.70]]
    p.write_text(
        json.dumps({
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [ring1]},
                },
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [ring2]},
                },
            ],
        }),
        encoding="utf-8",
    )
    load_polygon.cache_clear()
    poly = load_polygon(p)
    assert poly is not None
    # Two non-overlapping rectangles → MultiPolygon (or unioned geometry)
    assert poly.geom_type in ("Polygon", "MultiPolygon")


def test_point_in_polygon_inside(tmp_path: Path) -> None:
    """A point inside the box returns True."""
    p = tmp_path / "box2.geojson"
    _write_geojson(
        p,
        {
            "type": "Polygon",
            "coordinates": [
                [[-97.18, 33.61], [-97.10, 33.61], [-97.10, 33.65], [-97.18, 33.65], [-97.18, 33.61]]
            ],
        },
    )
    load_polygon.cache_clear()
    poly = load_polygon(p)
    # Centre of the box: lat=33.63, lon=-97.14
    assert point_in_polygon(33.63, -97.14, poly) is True


def test_point_in_polygon_outside(tmp_path: Path) -> None:
    """A point outside the box returns False."""
    p = tmp_path / "box3.geojson"
    _write_geojson(
        p,
        {
            "type": "Polygon",
            "coordinates": [
                [[-97.18, 33.61], [-97.10, 33.61], [-97.10, 33.65], [-97.18, 33.65], [-97.18, 33.61]]
            ],
        },
    )
    load_polygon.cache_clear()
    poly = load_polygon(p)
    # Well outside: lat=33.50, lon=-97.00
    assert point_in_polygon(33.50, -97.00, poly) is False


def test_point_in_polygon_missing_coords_returns_none(tmp_path: Path) -> None:
    """point_in_polygon returns None when lat or lon is None."""
    p = tmp_path / "box4.geojson"
    _write_geojson(
        p,
        {
            "type": "Polygon",
            "coordinates": [
                [[-97.18, 33.61], [-97.10, 33.61], [-97.10, 33.65], [-97.18, 33.65], [-97.18, 33.61]]
            ],
        },
    )
    load_polygon.cache_clear()
    poly = load_polygon(p)
    assert point_in_polygon(None, -97.14, poly) is None
    assert point_in_polygon(33.63, None, poly) is None
    assert point_in_polygon(None, None, poly) is None


def test_point_in_polygon_none_polygon_returns_none() -> None:
    """point_in_polygon returns None when polygon is None."""
    assert point_in_polygon(33.63, -97.14, None) is None
