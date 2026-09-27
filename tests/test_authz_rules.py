"""Authorization tests, the business rule the brief asks to be."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def _create(client, headers, title: str = "Owned Incident") -> str:
    response = await client.post(
        "/api/v1/disasters",
        headers=headers,
        json={
            "title": title,
            "description": "An incident in Mumbai created for authorization testing purposes.",
            "tags": ["authz"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# The headline rule


async def test_contributor_can_update_their_own_disaster(client, contributor_headers):
    disaster_id = await _create(client, contributor_headers)

    response = await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=contributor_headers,
        json={"status": "verified"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "verified"


async def test_contributor_cannot_update_someone_elses_disaster(
    client, contributor_headers, other_contributor_headers
):
    """The core ownership rule."""
    disaster_id = await _create(client, contributor_headers, "Belongs To First Contributor")

    response = await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=other_contributor_headers,
        json={"status": "resolved"},
    )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "forbidden"
    assert "only modify disasters you created" in body["detail"]

    after = await client.get(f"/api/v1/disasters/{disaster_id}")
    assert after.json()["status"] == "reported"


async def test_admin_can_update_any_disaster(client, contributor_headers, admin_headers):
    """Admin bypasses the ownership check, the other half of the same rule."""
    disaster_id = await _create(client, contributor_headers, "Created By Contributor")

    response = await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=admin_headers,
        json={"status": "in_progress"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"


# Role gates


async def test_only_admin_may_delete(client, contributor_headers, admin_headers):
    """Delete is admin-only, even for the record's own creator."""
    disaster_id = await _create(client, contributor_headers, "To Be Deleted")

    denied = await client.delete(f"/api/v1/disasters/{disaster_id}", headers=contributor_headers)
    assert denied.status_code == 403
    assert "requires one of: admin" in denied.json()["detail"]

    # Still there.
    assert (await client.get(f"/api/v1/disasters/{disaster_id}")).status_code == 200

    allowed = await client.delete(f"/api/v1/disasters/{disaster_id}", headers=admin_headers)
    assert allowed.status_code == 204
    assert (await client.get(f"/api/v1/disasters/{disaster_id}")).status_code == 404


async def test_responder_is_read_only(client, responder_headers, contributor_headers):
    """The responder role can read everything and write nothing."""
    denied = await client.post(
        "/api/v1/disasters",
        headers=responder_headers,
        json={
            "title": "Responder Attempt",
            "description": "An incident a read-only responder should not be able to create.",
        },
    )
    assert denied.status_code == 403
    assert "Your role is 'responder'" in denied.json()["detail"]

    # Reading is fine.
    disaster_id = await _create(client, contributor_headers, "Readable By Responder")
    assert (await client.get(f"/api/v1/disasters/{disaster_id}")).status_code == 200

    listing = await client.get("/api/v1/disasters", headers=responder_headers)
    assert listing.status_code == 200


async def test_resource_creation_is_admin_only(client, contributor_headers, admin_headers):
    payload = {
        "name": "Test Shelter",
        "type": "shelter",
        "latitude": 40.7128,
        "longitude": -74.0060,
    }

    denied = await client.post("/api/v1/resources", headers=contributor_headers, json=payload)
    assert denied.status_code == 403

    allowed = await client.post("/api/v1/resources", headers=admin_headers, json=payload)
    assert allowed.status_code == 201


async def test_deactivated_user_is_rejected_despite_valid_token(
    client, session, contributor_user, contributor_headers
):
    """Deactivation takes effect immediately, not when the token expires."""
    contributor_user.is_active = False
    await session.flush()

    response = await client.post(
        "/api/v1/disasters",
        headers=contributor_headers,
        json={
            "title": "Should Be Blocked",
            "description": "An incident attempted by a user who has since been deactivated.",
        },
    )

    assert response.status_code == 403
    assert "deactivated" in response.json()["detail"]


async def test_expired_token_is_rejected(client, contributor_user):
    from app.core.security import create_access_token

    token, _ = create_access_token(
        subject=str(contributor_user.id),
        username=contributor_user.username,
        role=str(contributor_user.role),
        expires_minutes=-1,  # already expired
    )

    response = await client.post(
        "/api/v1/disasters",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Expired", "description": "An incident posted with an expired token."},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Token has expired"


async def test_token_signed_with_wrong_secret_is_rejected(client, contributor_user):
    """Guards against the classic 'trust the payload, skip the signature' bug."""
    import jwt

    from app.core.config import settings

    forged = jwt.encode(
        {
            "sub": str(contributor_user.id),
            "username": contributor_user.username,
            "role": "admin",  # privilege escalation attempt
            "iat": 1,
            "exp": 9_999_999_999,
            "iss": "disaster-response-platform",
        },
        "the-attackers-own-secret",
        algorithm=settings.jwt_algorithm,
    )

    response = await client.post(
        "/api/v1/disasters",
        headers={"Authorization": f"Bearer {forged}"},
        json={"title": "Forged", "description": "An incident posted using a forged bearer token."},
    )

    assert response.status_code == 401


async def test_login_returns_token_and_public_user(client, contributor_user):
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "test_contributor", "password": "test-password-123"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["username"] == "test_contributor"
    assert body["user"]["role"] == "contributor"
    assert "password_hash" not in body["user"]


async def test_login_failure_does_not_reveal_whether_the_user_exists(client, contributor_user):
    """Identical response for a wrong password and an unknown username."""
    wrong_password = await client.post(
        "/api/v1/auth/login",
        json={"username": "test_contributor", "password": "wrong-password"},
    )
    unknown_user = await client.post(
        "/api/v1/auth/login",
        json={"username": "no_such_person", "password": "wrong-password"},
    )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"]
