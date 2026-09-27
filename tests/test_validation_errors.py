"""Validation and error-handling tests."""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def test_missing_required_fields_returns_422_problem_document(client, contributor_headers):
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={"title": "No description supplied"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    # RFC 9457 members
    assert body["status"] == 422
    assert body["code"] == "validation_error"
    assert body["type"].endswith("/validation_error")
    assert body["instance"] == "/api/v1/disasters"
    assert body["request_id"]
    assert any(e["field"].endswith("description") for e in body["errors"])


async def test_blank_title_is_rejected(client, contributor_headers):
    """A whitespace-only title passes a naive min_length check; it must not pass here."""
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "      ",
            "description": "A description of sufficient length about an incident in Mumbai.",
        },
    )
    assert response.status_code == 422
    assert any("title" in e["field"] for e in response.json()["errors"])


async def test_unknown_field_is_rejected_not_ignored(client, contributor_headers):
    """extra="forbid": a typo'd field should fail loudly, not vanish."""
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Typo Check",
            "description": "An incident in Chennai used to check unknown-field handling.",
            "statuss": "verified",
        },
    )
    assert response.status_code == 422
    assert any("statuss" in e["field"] for e in response.json()["errors"])


async def test_invalid_enum_value_is_rejected(client, contributor_headers):
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Bad Status",
            "description": "An incident in Mumbai with a status value that does not exist.",
            "status": "on_fire_probably",
        },
    )
    assert response.status_code == 422


async def test_latitude_out_of_range_is_rejected(client, contributor_headers):
    """Impossible coordinates are stopped at the type boundary, before PostGIS."""
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Impossible Coordinates",
            "description": "An incident with a latitude that cannot exist on Earth.",
            "latitude": 91.0,
            "longitude": 0.0,
        },
    )
    assert response.status_code == 422
    assert any("latitude" in e["field"] for e in response.json()["errors"])


async def test_lone_latitude_is_rejected(client, contributor_headers):
    """Half a coordinate pair is meaningless and is almost always a client bug."""
    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Half A Coordinate",
            "description": "An incident supplying only a latitude and no longitude value.",
            "latitude": 40.7,
        },
    )
    assert response.status_code == 422


async def test_unknown_disaster_returns_404_not_500(client):
    missing_id = uuid.uuid4()
    response = await client.get(f"/api/v1/disasters/{missing_id}")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert str(missing_id) in body["detail"]


async def test_malformed_uuid_returns_422(client):
    """A path parameter that is not a UUID is a validation error, not a 404 or 500."""
    response = await client.get("/api/v1/disasters/definitely-not-a-uuid")
    assert response.status_code == 422


async def test_negative_radius_is_rejected(client, contributor_headers):
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Radius Validation",
            "description": "An incident in Manhattan, NYC used to check radius validation.",
        },
    )
    disaster_id = created.json()["id"]

    response = await client.get(f"/api/v1/disasters/{disaster_id}/resources?radius_km=-5")
    assert response.status_code == 422


async def test_radius_above_cap_is_rejected(client, contributor_headers):
    """The cap stops one request from scanning the whole resource table."""
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Radius Cap",
            "description": "An incident in Manhattan, NYC used to check the maximum radius.",
        },
    )
    disaster_id = created.json()["id"]

    response = await client.get(f"/api/v1/disasters/{disaster_id}/resources?radius_km=99999")
    assert response.status_code == 422


async def test_empty_patch_body_is_rejected(client, contributor_headers):
    """A PATCH that changes nothing is a client mistake worth reporting."""
    created = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Empty Patch",
            "description": "An incident in Mumbai used to verify empty PATCH handling.",
        },
    )
    response = await client.patch(
        f"/api/v1/disasters/{created.json()['id']}",
        headers=contributor_headers,
        json={},
    )
    assert response.status_code == 422


async def test_missing_token_returns_401_with_www_authenticate(client):
    response = await client.post(
        "/api/v1/disasters",
        json={"title": "No Auth", "description": "An incident posted without a bearer token."},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers.get("www-authenticate") == "Bearer"


async def test_garbage_token_returns_401(client):
    response = await client.post(
        "/api/v1/disasters",
        headers={"Authorization": "Bearer not.a.real.token"},
        json={"title": "Bad Token", "description": "An incident posted with a forged token."},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid authentication token"


async def test_unrouted_path_returns_problem_json(client):
    """Even framework-generated 404s share the error envelope."""
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


async def test_every_response_carries_a_request_id(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


async def test_inbound_request_id_is_preserved_for_tracing(client):
    """An upstream trace id must survive, or cross-service correlation breaks."""
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc-123"})
    assert response.headers["X-Request-ID"] == "trace-abc-123"
