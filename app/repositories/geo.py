"""Geospatial helpers shared by the repositories.

This is the file to read if you want to understand how proximity search works
here. Two functions and one long explanation, because the *why* is the part that
matters.

Building the query point
------------------------
``ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography``

* ``ST_MakePoint`` takes **longitude first**. This is the single most common
  PostGIS mistake - it is (x, y), and x is longitude. Passing them the other way
  round produces a point that is silently valid and completely wrong, so the
  helper below takes named ``lat``/``lng`` arguments and swaps them itself.
* ``ST_SetSRID(..., 4326)`` tags the point as WGS 84 (plain GPS lat/lng). A
  point with no SRID cannot be compared against our stored column.
* ``::geography`` casts to the geography type, which is what makes distances
  come back in **metres on a sphere** rather than in degrees.

Why geography rather than geometry
----------------------------------
With ``geometry`` and SRID 4326, ``ST_Distance`` returns **degrees**, which are
not a distance: one degree of longitude is 111 km at the equator and 0 km at the
pole. Getting real metres from geometry means projecting into a local coordinate
system (UTM, say), which means choosing the right zone per query and handling
data that crosses zones. ``geography`` does the spheroid maths for us. It is a
little slower per comparison, and the index is doing the real work anyway.

Why ST_DWithin and not ST_Distance < r
--------------------------------------
This is the heart of it::

    -- Index-accelerated. The planner rewrites ST_DWithin into a bounding-box
    -- overlap test (&&) that the GiST index can answer, then runs the exact
    -- distance calculation only on the handful of surviving rows.
    WHERE ST_DWithin(location, :point, :radius_metres)

    -- NOT index-accelerated. ST_Distance must be evaluated for EVERY row before
    -- the comparison can be made, which is a sequential scan of the table.
    WHERE ST_Distance(location, :point) < :radius_metres

Same results, completely different plans. On a seeded table the difference is
invisible; at a million resources it is the difference between a millisecond and
a second. Verified with EXPLAIN ANALYZE - see ``docs/architecture.md``.

Ordering is a separate concern: ``ST_DWithin`` filters, it does not sort. The
sort uses ``ST_Distance`` on the already-filtered rows, which is cheap because
there are few of them left.
"""

from __future__ import annotations

from geoalchemy2 import Geography
from geoalchemy2.shape import to_shape
from sqlalchemy import Float, cast, func
from sqlalchemy.sql.elements import ColumnElement

# Metres per kilometre. Named because a bare 1000 in a spatial query is exactly
# the kind of thing that gets "optimised" into a bug.
METRES_PER_KM = 1000.0


def geography_point(lat: float, lng: float) -> ColumnElement:
    """Build a ``geography(Point, 4326)`` SQL expression from lat/lng.

    Note the deliberate swap: callers think in (lat, lng), PostGIS wants
    (x=lng, y=lat). Doing it once, here, means no call site can get it wrong.
    """
    return cast(func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326), Geography)


def within_radius(column: ColumnElement, lat: float, lng: float, radius_km: float) -> ColumnElement:
    """``ST_DWithin(column, point, radius_metres)`` - the index-using filter."""
    return func.ST_DWithin(column, geography_point(lat, lng), radius_km * METRES_PER_KM)


def distance_km(column: ColumnElement, lat: float, lng: float) -> ColumnElement:
    """Distance in kilometres, computed by PostGIS.

    Done in SQL rather than Python on purpose: the database can sort and limit by
    it, so ``ORDER BY distance LIMIT 20`` returns 20 rows instead of fetching
    every match and sorting in the application.
    """
    return cast(
        func.ST_Distance(column, geography_point(lat, lng)) / METRES_PER_KM,
        Float,
    )


def point_to_lat_lng(value: object) -> tuple[float, float] | None:
    """Convert a stored geography value into ``(latitude, longitude)``.

    SQLAlchemy hands back a ``WKBElement``; shapely decodes it. Shapely's ``.x``
    is longitude and ``.y`` is latitude - the same (x, y) convention as above,
    normalised back to (lat, lng) here so nothing outside this module has to
    remember it.
    """
    if value is None:
        return None
    try:
        shape = to_shape(value)
        return float(shape.y), float(shape.x)
    except Exception:  # noqa: BLE001 - a malformed point must not break serialization
        return None
