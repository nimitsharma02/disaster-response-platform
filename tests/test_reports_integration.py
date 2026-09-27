"""External integration tests: normalisation, caching and failure handling.

Covers the brief's "one external integration/mock". Every external call is
intercepted by ``httpx.MockTransport``, so the suite is offline and
deterministic - and, critically, the number of calls is *counted*, which is how
a cache hit is proved rather than assumed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

SOCIAL_PATH = "/social/search"
OFFICIAL_PATH = "/official/bulletins"


async def _create_disaster(client, headers) -> str:
    response = await client.post(
        "/api/v1/disasters",
        headers=headers,
        json={
            "title": "Reports Test Incident",
            "description": "Heavy flooding has affected Manhattan, NYC and reports are coming in.",
            "tags": ["flood"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


async def test_provider_payloads_are_normalised_into_one_shape(
    client, admin_headers, install_mock_http
):
    """Two different upstream shapes must converge on one contract.

    Social returns ``{post_id, content, user, created_at}``; official returns
    ``{ref, headline, body, issued_at, agency, severity}``. Nothing downstream
    should be able to tell which is which except via ``source``.
    """
    install_mock_http(social_posts=3)
    disaster_id = await _create_disaster(client, admin_headers)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert response.status_code == 200, response.text

    body = response.json()
    items = body["items"]
    assert len(items) == 4  # 3 social + 1 official

    for item in items:
        # Every item has the same keys regardless of provider.
        assert set(item) >= {
            "id", "source", "provider", "author", "content", "created_at",
            "priority", "priority_score", "matched_signals", "verified",
        }

    sources = {item["source"] for item in items}
    assert sources == {"social", "official"}

    official = next(i for i in items if i["source"] == "official")
    assert official["author"] == "Test Emergency Authority"
    assert official["verified"] is True  # agency-issued content is marked verified
    # Headline and body were merged into one content field.
    assert "Evacuation advisory issued" in official["content"]
    assert "relief centres" in official["content"]

    assert body["meta"]["sources"] == {"mock-social-api": "ok", "mock-official-api": "ok"}


async def test_reports_are_prioritised_and_sorted(client, admin_headers, install_mock_http):
    """The most urgent report must come first, with its reasoning attached."""
    install_mock_http(social_posts=3)
    disaster_id = await _create_disaster(client, admin_headers)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    items = response.json()["items"]

    top = items[0]
    assert top["priority"] == "critical"
    assert "trapped" in top["content"]
    # Explainability: the ranking must be auditable, not an opaque number.
    assert any("trapped" in signal for signal in top["matched_signals"])

    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    order = [rank[i["priority"]] for i in items]
    assert order == sorted(order)

    assert response.json()["meta"]["counts_by_priority"]["critical"] >= 1


async def test_priority_filter(client, admin_headers, install_mock_http):
    install_mock_http(social_posts=3)
    disaster_id = await _create_disaster(client, admin_headers)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports?priority=critical")
    assert all(i["priority"] == "critical" for i in response.json()["items"])


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


async def test_second_request_is_a_cache_hit_and_does_not_call_the_provider(
    client, admin_headers, install_mock_http
):
    """The cache requirement, proved by counting upstream calls.

    Asserting ``X-Cache: HIT`` alone would not prove anything - the header could
    be wrong. Counting calls to the mock transport proves the network was
    genuinely avoided.
    """
    calls = install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)

    first = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert first.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    assert calls[SOCIAL_PATH] == 1
    assert calls[OFFICIAL_PATH] == 1

    second = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert second.status_code == 200
    assert second.headers["X-Cache"] == "HIT"
    # The counters have NOT moved: no external call was made.
    assert calls[SOCIAL_PATH] == 1
    assert calls[OFFICIAL_PATH] == 1

    assert first.json()["items"] == second.json()["items"]


async def test_refresh_parameter_bypasses_the_cache(client, admin_headers, install_mock_http):
    calls = install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)

    await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert calls[SOCIAL_PATH] == 1

    forced = await client.get(f"/api/v1/disasters/{disaster_id}/reports?refresh=true")
    assert forced.status_code == 200
    assert calls[SOCIAL_PATH] == 2  # deliberately re-fetched


async def test_cache_is_scoped_per_disaster(client, admin_headers, install_mock_http):
    """One incident's cached reports must never be served for another."""
    calls = install_mock_http(social_posts=2)

    first_id = await _create_disaster(client, admin_headers)
    second = await client.post(
        "/api/v1/disasters",
        headers=admin_headers,
        json={
            "title": "A Different Incident",
            "description": "Severe waterlogging reported across Mumbai after heavy rainfall.",
        },
    )
    second_id = second.json()["id"]

    await client.get(f"/api/v1/disasters/{first_id}/reports")
    assert calls[SOCIAL_PATH] == 1

    response = await client.get(f"/api/v1/disasters/{second_id}/reports")
    assert response.headers["X-Cache"] == "MISS"
    assert calls[SOCIAL_PATH] == 2


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


async def test_one_provider_down_still_returns_the_others_reports(
    client, admin_headers, install_mock_http
):
    """Partial failure degrades the response; it does not fail it.

    ``asyncio.gather(..., return_exceptions=True)`` is what makes this work -
    without it the failing task would cancel its sibling and the working
    provider's data would be discarded too.
    """
    install_mock_http(social_posts=3, official_status=503)
    disaster_id = await _create_disaster(client, admin_headers)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")

    assert response.status_code == 200, "a single provider failure must not fail the request"
    body = response.json()

    assert len(body["items"]) == 3
    assert all(i["source"] == "social" for i in body["items"])

    assert body["meta"]["degraded"] is True
    assert body["meta"]["sources"]["mock-social-api"] == "ok"
    assert body["meta"]["sources"]["mock-official-api"] == "failed"
    assert response.headers.get("X-Degraded") == "true"


async def test_all_providers_down_serves_stale_cache_rather_than_failing(
    client, admin_headers, install_mock_http
):
    """The stale-while-revalidate path.

    Five-minute-old reports are far more useful to a responder than a 502, so a
    total upstream outage is answered from the stale window with an explicit
    ``degraded`` flag rather than an error.
    """
    from app.cache import cache

    install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)

    warm = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert warm.headers["X-Cache"] == "MISS"
    cached_items = warm.json()["items"]

    # Force the entry past its soft expiry while leaving it inside the stale
    # window, then take both providers down.
    key = cache.make_key("reports", disaster_id)
    value = await cache.get(key)
    await cache.set(key, value, ttl=0, stale_grace=3600)
    cache.clear_l1()

    install_mock_http(social_status=503, official_status=503)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")

    assert response.status_code == 200, "stale data beats an error for a responder"
    assert response.headers["X-Cache"] == "STALE"
    assert response.json()["meta"]["degraded"] is True
    assert response.json()["items"] == cached_items


async def test_all_providers_down_with_a_cold_cache_returns_502(
    client, admin_headers, install_mock_http
):
    """The one case this endpoint is allowed to fail: nothing cached, nothing up.

    502 rather than 500 - the fault is upstream, and the status code should say so.
    """
    install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)

    install_mock_http(social_status=503, official_status=503)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")

    assert response.status_code == 502
    assert response.json()["code"] == "upstream_unavailable"


async def test_a_failing_provider_never_breaks_disaster_crud(
    client, admin_headers, install_mock_http
):
    """Blast-radius containment.

    The external dependency lives behind the reports endpoint only. A dead
    social API must not affect the ability to record or read incidents.
    """
    install_mock_http(social_status=503, official_status=503)

    created = await client.post(
        "/api/v1/disasters",
        headers=admin_headers,
        json={
            "title": "Created While Providers Are Down",
            "description": "Heavy flooding has affected Manhattan, NYC during a provider outage.",
        },
    )
    assert created.status_code == 201

    assert (await client.get(f"/api/v1/disasters/{created.json()['id']}")).status_code == 200
    assert (await client.get("/api/v1/disasters")).status_code == 200


async def test_circuit_breaker_opens_after_repeated_failures(
    client, admin_headers, install_mock_http
):
    """After enough consecutive failures the breaker stops calling entirely.

    The observable effect is that upstream call counts stop increasing: we are
    now failing fast instead of spending the timeout budget on a dead service.
    """
    from app.core.config import settings

    # Warm the cache first, so that once the breaker opens there is still stale
    # data to serve. Without it the response would be a 502 (correct, but it
    # would hide the `circuit_open` marker this test is about).
    install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)
    await client.get(f"/api/v1/disasters/{disaster_id}/reports")

    # Now take both providers down.
    calls = install_mock_http(social_status=503, official_status=503)

    for _ in range(settings.circuit_fail_threshold + 2):
        await client.get(f"/api/v1/disasters/{disaster_id}/reports?refresh=true")

    calls_after_trip = calls.get(SOCIAL_PATH, 0)

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports?refresh=true")

    assert calls.get(SOCIAL_PATH, 0) == calls_after_trip, (
        "the breaker is open, so no further upstream calls should be attempted"
    )
    # Failing fast still serves the cached payload rather than an error, and says
    # explicitly that the provider was skipped rather than merely unreachable.
    assert response.status_code == 200
    assert response.json()["meta"]["sources"]["mock-social-api"] == "circuit_open"
    assert response.json()["meta"]["degraded"] is True


async def test_malformed_provider_payload_is_handled(client, admin_headers):
    """A provider returning nonsense must not produce a 500.

    External data is untrusted input: a contract break should surface as a
    handled upstream error, not an unhandled exception.
    """
    import httpx

    from app.integrations import http as http_module

    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK, but the documented keys are absent.
        return httpx.Response(200, json={"unexpected": "shape"})

    http_module.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    created = await client.post(
        "/api/v1/disasters",
        headers=admin_headers,
        json={
            "title": "Malformed Upstream",
            "description": "Heavy flooding has affected Manhattan, NYC with a broken provider.",
        },
    )
    disaster_id = created.json()["id"]

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_unavailable"


async def test_updating_the_description_invalidates_the_reports_cache(
    client, admin_headers, install_mock_http
):
    """The provider query is derived from the disaster, so a change must evict.

    Otherwise the endpoint would keep serving reports for the old location.
    """
    calls = install_mock_http(social_posts=2)
    disaster_id = await _create_disaster(client, admin_headers)

    await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert calls[SOCIAL_PATH] == 1

    await client.patch(
        f"/api/v1/disasters/{disaster_id}",
        headers=admin_headers,
        json={"description": "Severe waterlogging reported across Mumbai after heavy rainfall."},
    )

    response = await client.get(f"/api/v1/disasters/{disaster_id}/reports")
    assert response.headers["X-Cache"] == "MISS"
    assert calls[SOCIAL_PATH] == 2
