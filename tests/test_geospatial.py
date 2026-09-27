"""Geospatial tests: radius filtering, distance accuracy and ordering.

These run against real PostGIS. Mocking the database here would test the mock -
the entire claim being verified is that ``ST_DWithin`` on a
``geography(Point, 4326)`` column returns the right rows with the right
distances.

Fixture geometry (from ``conftest.geo_resources``), measured from the NYC
reference point (40.7128, -74.0060):

    very_close  ~0.03 km
    close       ~1.13 km
    mid         ~4.45 km
    far         ~8.50 km
    very_far    ~5570 km   (London)
"""

from __future__ import annotations

import pytest

from tests.conftest import NYC_LAT, NYC_LNG

pytestmark = pytest.mark.asyncio


async def _create_disaster_at(client, headers, lat: float, lng: float, title="Geo Incident") -> str:
    response = await client.post(
        "/api/v1/disasters",
        headers=headers,
        json={
            "title": title,
            "description": "An incident created with explicit coordinates for geospatial tests.",
            "latitude": lat,
            "longitude": lng,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_radius_includes_only_resources_inside_it(
    client, admin_headers, geo_resources
):
    """A 2 km radius must return exactly the two resources within 2 km."""
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources?radius_km=2&limit=100"
    )
    assert response.status_code == 200, response.text

    body = response.json()
    names = {item["name"] for item in body["items"]}

    assert "geo_very_close" in names   # ~0.03 km
    assert "geo_close" in names        # ~1.13 km
    assert "geo_mid" not in names      # ~4.45 km - outside
    assert "geo_far" not in names      # ~8.50 km - outside
    assert "geo_very_far" not in names  # London
    assert body["count"] == 2


async def test_widening_the_radius_includes_more(client, admin_headers, geo_resources):
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    for radius, expected in [(0.5, 1), (2, 2), (5, 3), (10, 4)]:
        response = await client.get(
            f"/api/v1/disasters/{disaster_id}/resources?radius_km={radius}&limit=100"
        )
        assert response.json()["count"] == expected, f"radius {radius} km"


async def test_results_are_ordered_nearest_first(client, admin_headers, geo_resources):
    """Ordering happens in SQL, so LIMIT returns the genuinely nearest rows."""
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources?radius_km=20&limit=100"
    )
    items = response.json()["items"]

    distances = [item["distance_km"] for item in items]
    assert distances == sorted(distances), "results must come back nearest-first"
    assert items[0]["name"] == "geo_very_close"


async def test_distance_is_accurate_in_kilometres(client, admin_headers, geo_resources):
    """Distances must be real metres on a spheroid, not degrees.

    ``geo_close`` is 0.0102 degrees of latitude north of the reference point.
    One degree of latitude is ~111.2 km, so the expected distance is ~1.13 km.
    A geometry column would have returned 0.0102 here - the number that catches
    a geography/geometry mix-up immediately.
    """
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources?radius_km=20&limit=100"
    )
    by_name = {item["name"]: item for item in response.json()["items"]}

    assert by_name["geo_close"]["distance_km"] == pytest.approx(1.134, abs=0.05)
    assert by_name["geo_mid"]["distance_km"] == pytest.approx(4.448, abs=0.10)
    assert by_name["geo_far"]["distance_km"] == pytest.approx(8.507, abs=0.15)


async def test_search_centre_defaults_to_the_disaster_location(
    client, admin_headers, geo_resources
):
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/resources?radius_km=2")
    body = response.json()

    assert body["center_source"] == "disaster"
    assert body["center"]["latitude"] == pytest.approx(NYC_LAT, abs=1e-4)
    assert body["center"]["longitude"] == pytest.approx(NYC_LNG, abs=1e-4)


async def test_explicit_centre_overrides_the_disaster_location(
    client, admin_headers, geo_resources
):
    """A responder searching around their own position, not the incident's."""
    # Incident in London; search around New York.
    disaster_id = await _create_disaster_at(client, admin_headers, 51.5074, -0.1278, "London Event")

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources"
        f"?lat={NYC_LAT}&lng={NYC_LNG}&radius_km=2&limit=100"
    )
    body = response.json()

    assert body["center_source"] == "query"
    assert {i["name"] for i in body["items"]} == {"geo_very_close", "geo_close"}


async def test_type_filter_combines_with_the_radius(client, admin_headers, geo_resources):
    disaster_id = await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG)

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources?radius_km=20&type=hospital&limit=100"
    )
    items = response.json()["items"]

    assert len(items) == 1
    assert items[0]["name"] == "geo_mid"
    assert items[0]["type"] == "hospital"


async def test_disaster_without_location_returns_422_not_empty_list(client, admin_headers):
    """"We do not know where to look" must not be reported as "nothing nearby"."""
    created = await client.post(
        "/api/v1/disasters",
        headers=admin_headers,
        json={
            "title": "Nowhere In Particular",
            "description": "an incident whose description names no identifiable place at all",
        },
    )
    disaster_id = created.json()["id"]
    assert created.json()["location"]["point"] is None

    response = await client.get(f"/api/v1/disasters/{disaster_id}/resources?radius_km=5")

    assert response.status_code == 422
    assert "no resolved location" in response.json()["detail"]


async def test_antimeridian_is_handled_correctly(client, admin_headers, session):
    """Two points either side of the 180th meridian are ~112 km apart, not ~19,000.

    This is the case that breaks naive bounding-box or flat-earth distance
    maths, and it is the clearest justification for using the geography type
    rather than doing the arithmetic ourselves.
    """
    from app.repositories import resource_repo

    await resource_repo.create(
        session,
        name="west_of_antimeridian",
        resource_type="shelter",
        latitude=0.0,
        longitude=179.5,
    )
    await resource_repo.create(
        session,
        name="east_of_antimeridian",
        resource_type="shelter",
        latitude=0.0,
        longitude=-179.5,
    )
    await session.flush()

    disaster_id = await _create_disaster_at(client, admin_headers, 0.0, 179.5, "Pacific Incident")

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/resources?radius_km=200&limit=100"
    )
    names = {i["name"] for i in response.json()["items"]}

    assert "west_of_antimeridian" in names
    assert "east_of_antimeridian" in names, (
        "the point across the antimeridian must be found; "
        "if this fails, distance is being computed on a flat plane"
    )


async def test_disaster_list_supports_proximity_filtering(client, admin_headers):
    """The same ST_DWithin path, applied to the disaster list endpoint."""
    await _create_disaster_at(client, admin_headers, NYC_LAT, NYC_LNG, "Near Incident")
    await _create_disaster_at(client, admin_headers, 51.5074, -0.1278, "Far Incident")

    response = await client.get(
        f"/api/v1/disasters?lat={NYC_LAT}&lng={NYC_LNG}&radius_km=50&sort=distance"
    )
    assert response.status_code == 200

    titles = [item["title"] for item in response.json()["items"]]
    assert "Near Incident" in titles
    assert "Far Incident" not in titles

    assert response.json()["items"][0]["distance_km"] is not None
