"""Geospatial helpers shared by the repositories."""

from __future__ import annotations

from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from sqlalchemy import Float, cast, func
from sqlalchemy.sql.elements import ColumnElement

METRES_PER_KM = 1000.0


def geography_point(lat: float, lng: float) -> ColumnElement:
    """Build a geography(Point, 4326) SQL expression from lat/lng."""
    return cast(func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326), Geography)


def within_radius(column: ColumnElement, lat: float, lng: float, radius_km: float) -> ColumnElement:
    """ST_DWithin(column, point, radius_metres), the index-using filter."""
    return func.ST_DWithin(column, geography_point(lat, lng), radius_km * METRES_PER_KM)


def distance_km(column: ColumnElement, lat: float, lng: float) -> ColumnElement:
    """Distance in kilometres, computed by PostGIS."""
    return cast(
        func.ST_Distance(column, geography_point(lat, lng)) / METRES_PER_KM,
        Float,
    )


def point_to_lat_lng(value: object) -> tuple[float, float] | None:
    """Convert a stored geography value into (latitude, longitude)."""
    if value is None:
        return None
    try:
        shape = to_shape(value)
        return float(shape.y), float(shape.x)
    except Exception:
        return None
