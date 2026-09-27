"""API endpoint tests: disaster CRUD, filtering and location resolution.

Covers the brief's "test at least one API endpoint".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_create_disaster_resolves_location_from_description(client, contributor_headers):
    """The headline behaviour: a location is derived from free text.

    Uses the exact example from the brief. No coordinates are sent; the service
    must extract "Manhattan, NYC" and resolve it to a point. With
    GEOCODER_ENABLED=false in the test environment this exercises the offline
    path end to end, which is also the path a reviewer gets with no API keys.
    """
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Manhattan Flash Flooding",
            "description": "Heavy flooding has affected Manhattan, NYC. "
                           "Several subway stations are submerged.",
            "tags": ["flood", "urgent"],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()

    assert body["title"] == "Manhattan Flash Flooding"
    assert body["status"] == "reported"
    assert body["tags"] == ["flood", "urgent"]

    location = body["location"]
    assert location["point"] is not None, "location should have been resolved"
    # Manhattan is around 40.78 N, -73.97 E. Generous bounds: we are asserting
    # that resolution landed in the right place, not testing the gazetteer's
    # precision.
    assert 40.5 < location["point"]["latitude"] < 41.0
    assert -74.2 < location["point"]["longitude"] < -73.7
    assert location["source"] is not None and location["source"] != "unresolved"
    assert 0 < location["confidence"] <= 1

    # Provenance must be reported, not silently implied.
    assert "gazetteer" in location["source"] or "nominatim" in location["source"]


async def test_create_disaster_accepts_explicit_coordinates(client, contributor_headers):
    """Explicit coordinates override inference and are marked as manual."""
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Custom Location Incident",
            "description": "An incident at a location supplied directly by the reporter.",
            "latitude": 12.3456,
            "longitude": 65.4321,
            "location_name": "Survey Point Alpha",
        },
    )

    assert response.status_code == 201, response.text
    location = response.json()["location"]
    assert location["point"]["latitude"] == pytest.approx(12.3456, abs=1e-4)
    assert location["point"]["longitude"] == pytest.approx(65.4321, abs=1e-4)
    assert location["source"] == "manual"
    assert location["confidence"] == 1.0


async def test_create_disaster_without_recognisable_place_still_succeeds(
    client, contributor_headers
):
    """Resolution failure must never block recording an incident.

    This is a deliberate product decision: a report filed under pressure with a
    vague description is still worth having.
    """
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Unknown Location Event",
            "description": "something bad is happening and nobody knows exactly where it is",
            "tags": ["unknown"],
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["location"]["point"] is None


async def test_list_disasters_filters_by_tag(client, contributor_headers):
    """``GET /disasters?tag=flood`` - the filter named in the brief."""
    for title, tags in [
        ("Flood One", ["flood", "urgent"]),
        ("Flood Two", ["flood"]),
        ("Fire One", ["fire"]),
    ]:
        created = await client.post(
            "/api/v1/disasters",
            headers=contributor_headers,
            json={
                "title": title,
                "description": f"A test incident in Mumbai for {title} with enough description.",
                "tags": tags,
            },
        )
        assert created.status_code == 201, created.text

    response = await client.get("/api/v1/disasters?tag=flood")
    assert response.status_code == 200

    body = response.json()
    titles = {item["title"] for item in body["items"]}
    assert titles == {"Flood One", "Flood Two"}
    assert body["meta"]["total"] == 2
    assert all("flood" in item["tags"] for item in body["items"])


async def test_tags_are_normalised_so_filtering_is_reliable(client, contributor_headers):
    """"Flood", " FLOOD " and "flood" must be one tag, or filters silently miss rows."""
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Tag Normalisation Check",
            "description": "An incident in Chennai used to verify tag normalisation behaviour.",
            "tags": ["Flood", " FLOOD ", "Storm Surge", "flood"],
        },
    )
    assert created.status_code == 201, created.text
    # Lowercased, spaces to hyphens, duplicates collapsed, order preserved.
    assert created.json()["tags"] == ["flood", "storm-surge"]

    found = await client.get("/api/v1/disasters?tag=flood")
    assert created.json()["id"] in {i["id"] for i in found.json()["items"]}


async def test_get_disaster_by_id(client, contributor_headers):
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Fetch Me",
            "description": "An incident in Kathmandu created so it can be fetched by id.",
        },
    )
    disaster_id = created.json()["id"]

    response = await client.get(f"/api/v1/disasters/{disaster_id}")
    assert response.status_code == 200
    assert response.json()["id"] == disaster_id
    assert response.json()["creator"]["username"] == "test_contributor"


async def test_patch_updates_only_supplied_fields(client, contributor_headers):
    """PATCH semantics: an omitted field is left alone, not reset to a default."""
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Original Title",
            "description": "An incident in Mumbai with an original description for patching.",
            "tags": ["flood"],
        },
    )
    disaster_id = created.json()["id"]
    original_description = created.json()["description"]

    response = await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=contributor_headers,
        json={"status": "verified"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified"
    assert body["title"] == "Original Title"          # untouched
    assert body["description"] == original_description  # untouched
    assert body["tags"] == ["flood"]                   # untouched


async def test_pagination_reports_accurate_total(client, contributor_headers):
    for index in range(5):
        await client.post(
            "/api/v1/disasters",
            headers=contributor_headers,
            json={
                "title": f"Paged Incident {index}",
                "description": f"Incident number {index} in Chennai for pagination testing.",
                "tags": ["paging"],
            },
        )

    response = await client.get("/api/v1/disasters?tag=paging&limit=2&offset=0")
    body = response.json()

    assert len(body["items"]) == 2
    # total must reflect every matching row, not the page size.
    assert body["meta"]["total"] == 5
    assert body["meta"]["has_more"] is True

    last_page = await client.get("/api/v1/disasters?tag=paging&limit=2&offset=4")
    assert len(last_page.json()["items"]) == 1
    assert last_page.json()["meta"]["has_more"] is False


async def test_audit_trail_records_the_change(client, contributor_headers):
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Audited Incident",
            "description": "An incident in Mumbai created to verify the audit trail works.",
        },
    )
    disaster_id = created.json()["id"]

    await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=contributor_headers,
        json={"status": "in_progress"},
    )

    response = await client.get(
        f"/api/v1/disasters/{disaster_id}/audit", headers=contributor_headers
    )
    assert response.status_code == 200

    entries = response.json()
    actions = [e["action"] for e in entries]
    assert "create" in actions
    assert "update" in actions

    update_entry = next(e for e in entries if e["action"] == "update")
    assert update_entry["changes"]["status"] == {"from": "reported", "to": "in_progress"}
    assert update_entry["actor"] == "test_contributor"
