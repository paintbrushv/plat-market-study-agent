"""City-limits polygon loader + point-in-polygon helper."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry


@cache
def load_polygon(geojson_path: Path) -> BaseGeometry | None:
    """Load a GeoJSON FeatureCollection and return a unioned polygon, or None
    if the file is missing/empty.
    """
    if not geojson_path.exists():
        return None
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = data.get("features") or []
    if not features:
        return None
    geoms = [shape(f["geometry"]) for f in features if f.get("geometry")]
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    # Union if multiple features (rare for a single city).
    from shapely.ops import unary_union

    return unary_union(geoms)


def point_in_polygon(
    lat: float | None,
    lon: float | None,
    polygon: BaseGeometry | None,
) -> bool | None:
    """Return True/False/None. None when polygon or coords are missing."""
    if polygon is None or lat is None or lon is None:
        return None
    return polygon.contains(Point(lon, lat))  # GeoJSON is (lon, lat) order
