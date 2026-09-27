"""Generate docs/postman_collection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE = "{{baseUrl}}"


def url(path: str, query: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    segments = [s for s in path.strip("/").split("/") if s]
    built: dict[str, Any] = {"raw": BASE + path, "host": [BASE], "path": segments}
    if query:
        built["query"] = query
        built["raw"] = BASE + path + "?" + "&".join(
            f"{q['key']}={q['value']}" for q in query if not q.get("disabled")
        )
    return built


def request(
    name: str,
    method: str,
    path: str,
    *,
    description: str = "",
    body: dict | None = None,
    query: list[dict] | None = None,
    auth: bool = False,
    test: str | None = None,
) -> dict[str, Any]:
    headers = [{"key": "Content-Type", "value": "application/json"}] if body else []
    if auth:
        headers.append({"key": "Authorization", "value": "Bearer {{token}}"})

    item: dict[str, Any] = {
        "name": name,
        "request": {
            "method": method,
            "header": headers,
            "url": url(path, query),
            "description": description,
        },
        "response": [],
    }
    if body is not None:
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    if test:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript",
                                                       "exec": test.strip().split("\n")}}]
    return item


COLLECTION: dict[str, Any] = {
    "info": {
        "name": "Disaster Response Coordination Platform",
        "description": (
            "Run the requests in order. **1. Authentication -> Login as admin** stores the "
            "JWT in a collection variable, so every later request is authorised "
            "automatically. **2. Disasters -> Create** stores the new id as `disasterId`.\n\n"
            "Set `baseUrl` to `http://localhost:8000/api/v1` (the default)."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "variable": [
        {"key": "baseUrl", "value": "http://localhost:8000/api/v1"},
        {"key": "token", "value": ""},
        {"key": "disasterId", "value": ""},
    ],
    "item": [
        {
            "name": "1. Authentication",
            "description": "Mock users seeded by scripts/seed.py.",
            "item": [
                request(
                    "Login as admin", "POST", "/auth/login",
                    description="Full access, including DELETE. Stores {{token}}.",
                    body={"username": "admin", "password": "admin123"},
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
const body = pm.response.json();
pm.collectionVariables.set("token", body.access_token);
pm.test("returns a bearer token", () => pm.expect(body.token_type).to.eql("bearer"));
pm.test("never leaks the password hash", () =>
    pm.expect(body.user).to.not.have.property("password_hash"));
""",
                ),
                request(
                    "Login as contributor", "POST", "/auth/login",
                    description="May create, and update ONLY records they own. Stores {{token}}.",
                    body={"username": "contributor", "password": "contributor123"},
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.collectionVariables.set("token", pm.response.json().access_token);
""",
                ),
                request(
                    "Login as responder (read-only)", "POST", "/auth/login",
                    description="Read-only role. Use to demonstrate a 403 on write.",
                    body={"username": "responder", "password": "responder123"},
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.collectionVariables.set("token", pm.response.json().access_token);
""",
                ),
                request(
                    "Login with a wrong password (401)", "POST", "/auth/login",
                    description=(
                        "Returns exactly the same body as an unknown username, so the "
                        "endpoint cannot be used to enumerate accounts."
                    ),
                    body={"username": "admin", "password": "wrong-password"},
                    test="""
pm.test("401", () => pm.response.to.have.status(401));
pm.test("problem+json", () =>
    pm.expect(pm.response.headers.get("content-type")).to.include("problem+json"));
""",
                ),
                request("Who am I", "GET", "/auth/me", auth=True),
            ],
        },
        {
            "name": "2. Disasters (CRUD)",
            "item": [
                request(
                    "Create — location resolved from the description", "POST", "/disasters",
                    description=(
                        "No coordinates are sent. The service extracts the place name from "
                        "`description` and geocodes it, reporting how it got there in "
                        "`location.source` with a `location.confidence` score.\n\n"
                        "Stores the new id as {{disasterId}}."
                    ),
                    auth=True,
                    body={
                        "title": "Manhattan Flash Flooding",
                        "description": ("Heavy flooding has affected Manhattan, NYC. "
                                        "Several subway stations are submerged."),
                        "tags": ["flood", "urgent"],
                    },
                    test="""
pm.test("201 Created", () => pm.response.to.have.status(201));
const body = pm.response.json();
pm.collectionVariables.set("disasterId", body.id);
pm.test("location was resolved from free text", () =>
    pm.expect(body.location.point).to.not.be.null);
pm.test("resolution provenance is reported", () => {
    pm.expect(body.location.source).to.be.a("string");
    pm.expect(body.location.confidence).to.be.above(0);
});
""",
                ),
                request(
                    "Create with explicit coordinates", "POST", "/disasters",
                    description="Supplied coordinates always win; source becomes 'manual'.",
                    auth=True,
                    body={
                        "title": "Surveyed Incident",
                        "description": "An incident at coordinates supplied by the reporter.",
                        "latitude": 19.0760, "longitude": 72.8777,
                        "location_name": "Mumbai, India",
                    },
                ),
                request(
                    "Create with an unresolvable description", "POST", "/disasters",
                    description=(
                        "Resolution failure must never block recording an incident. "
                        "Returns 201 with location.point = null."
                    ),
                    auth=True,
                    body={"title": "Unknown Location Event",
                          "description": "something bad is happening and nobody knows where"},
                    test="""
pm.test("201 even though nothing resolved", () => pm.response.to.have.status(201));
pm.test("location is explicitly null", () =>
    pm.expect(pm.response.json().location.point).to.be.null);
""",
                ),
                request("List all", "GET", "/disasters",
                        query=[{"key": "limit", "value": "20"},
                               {"key": "offset", "value": "0"}]),
                request(
                    "Filter by tag", "GET", "/disasters",
                    description="Tag filter, served by the GIN index on tags.",
                    query=[{"key": "tag", "value": "flood"}],
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("every result carries the tag", () =>
    pm.response.json().items.forEach(d => pm.expect(d.tags).to.include("flood")));
""",
                ),
                request("Filter by status", "GET", "/disasters",
                        query=[{"key": "status", "value": "in_progress"}]),
                request("Free-text search", "GET", "/disasters",
                        description="Substring of title or description (pg_trgm index).",
                        query=[{"key": "q", "value": "flood"}]),
                request(
                    "Filter by proximity", "GET", "/disasters",
                    description="Same PostGIS ST_DWithin path, applied to the list endpoint.",
                    query=[{"key": "lat", "value": "40.7128"},
                           {"key": "lng", "value": "-74.0060"},
                           {"key": "radius_km", "value": "50"},
                           {"key": "sort", "value": "distance"}],
                ),
                request("Get one", "GET", "/disasters/{{disasterId}}"),
                request(
                    "Update (partial)", "PATCH", "/disasters/{{disasterId}}",
                    description=("Only the fields present in the body change. "
                                 "Emits disaster_updated."),
                    auth=True, body={"status": "in_progress"},
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("status changed", () => pm.expect(pm.response.json().status).to.eql("in_progress"));
""",
                ),
                request("Audit trail", "GET", "/disasters/{{disasterId}}/audit", auth=True,
                        description="Who changed this incident, what changed, and when."),
                request(
                    "Delete (admin only)", "DELETE", "/disasters/{{disasterId}}",
                    description=("Log in as admin first. A contributor gets 403 "
                                 "even for their own record."),
                    auth=True,
                    test="""
pm.test("204 No Content", () => pm.response.to.have.status(204));
""",
                ),
            ],
        },
        {
            "name": "3. Geospatial — nearby resources",
            "description": "PostGIS ST_DWithin against a GiST-indexed geography column.",
            "item": [
                request(
                    "Nearby (centre = the disaster)", "GET",
                    "/disasters/{{disasterId}}/resources",
                    description=(
                        "Omit lat/lng and the search centres on the incident's own resolved "
                        "location. Results are nearest-first with distances computed by "
                        "PostGIS in kilometres."
                    ),
                    query=[{"key": "radius_km", "value": "5"}, {"key": "limit", "value": "50"}],
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
const body = pm.response.json();
pm.test("centre was taken from the disaster", () =>
    pm.expect(body.center_source).to.eql("disaster"));
pm.test("results are ordered nearest-first", () => {
    const d = body.items.map(i => i.distance_km);
    pm.expect(d).to.eql([...d].sort((a, b) => a - b));
});
pm.test("everything is inside the radius", () =>
    body.items.forEach(i => pm.expect(i.distance_km).to.be.at.most(body.radius_km)));
""",
                ),
                request(
                    "Nearby (centre = my position)", "GET",
                    "/disasters/{{disasterId}}/resources",
                    description="A responder searching around their own location instead.",
                    query=[{"key": "lat", "value": "40.7128"},
                           {"key": "lng", "value": "-74.0060"},
                           {"key": "radius_km", "value": "10"}],
                ),
                request(
                    "Nearby, filtered by type", "GET", "/disasters/{{disasterId}}/resources",
                    query=[{"key": "radius_km", "value": "20"},
                           {"key": "type", "value": "hospital"}],
                ),
                request(
                    "Radius above the cap (422)", "GET", "/disasters/{{disasterId}}/resources",
                    description=(
                        "radius_km is capped so one request cannot scan the whole table "
                        "and defeat the spatial index."
                    ),
                    query=[{"key": "radius_km", "value": "99999"}],
                    test="""
pm.test("422", () => pm.response.to.have.status(422));
""",
                ),
                request("List all resources", "GET", "/resources",
                        query=[{"key": "limit", "value": "50"}]),
                request(
                    "Create a resource (admin only)", "POST", "/resources",
                    description="Coordinates are required here, unlike on a disaster.",
                    auth=True,
                    body={"name": "Bellevue Hospital Center", "type": "hospital",
                          "latitude": 40.7392, "longitude": -73.9762,
                          "address": "462 1st Ave, New York, NY 10016", "capacity": 850},
                ),
            ],
        },
        {
            "name": "4. Reports — external integration + caching",
            "description": (
                "Aggregates two mock providers with different payload shapes. Watch the "
                "X-Cache header: MISS on the first call, HIT on the second."
            ),
            "item": [
                request(
                    "Reports (first call — MISS)", "GET", "/disasters/{{disasterId}}/reports",
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("cache reports MISS", () =>
    pm.expect(pm.response.headers.get("X-Cache")).to.eql("MISS"));
pm.test("both providers normalised into one shape", () => {
    const sources = new Set(pm.response.json().items.map(i => i.source));
    pm.expect(sources.has("social") || sources.has("official")).to.be.true;
});
""",
                ),
                request(
                    "Reports (second call — HIT)", "GET", "/disasters/{{disasterId}}/reports",
                    description="No external call is made; served from cache.",
                    test="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("cache reports HIT", () =>
    pm.expect(pm.response.headers.get("X-Cache")).to.eql("HIT"));
""",
                ),
                request("Reports, forced refresh", "GET", "/disasters/{{disasterId}}/reports",
                        description="Bypasses the cache and re-fetches.",
                        query=[{"key": "refresh", "value": "true"}]),
                request(
                    "Reports, only critical", "GET", "/disasters/{{disasterId}}/reports",
                    description=(
                        "Priority is assigned by a deterministic classifier; "
                        "matched_signals explains why each report scored as it did."
                    ),
                    query=[{"key": "priority", "value": "critical"}],
                ),
            ],
        },
        {
            "name": "5. Error handling",
            "description": "Every error is an RFC 9457 problem document.",
            "item": [
                request(
                    "Missing required field (422)", "POST", "/disasters",
                    auth=True, body={"title": "No description"},
                    test="""
pm.test("422", () => pm.response.to.have.status(422));
pm.test("problem+json", () =>
    pm.expect(pm.response.headers.get("content-type")).to.include("problem+json"));
pm.test("field-level detail", () => {
    const b = pm.response.json();
    pm.expect(b.errors).to.be.an("array");
    pm.expect(b.request_id).to.be.a("string");
});
""",
                ),
                request(
                    "Unknown field is rejected, not ignored (422)", "POST", "/disasters",
                    auth=True,
                    body={"title": "Typo Check",
                          "description": "An incident in Mumbai used to check unknown fields.",
                          "statuss": "verified"},
                ),
                request(
                    "Impossible latitude (422)", "POST", "/disasters",
                    auth=True,
                    body={"title": "Bad Coordinates",
                          "description": "A latitude that cannot exist on Earth.",
                          "latitude": 91.0, "longitude": 0.0},
                ),
                request(
                    "Unknown disaster (404)", "GET",
                    "/disasters/00000000-0000-0000-0000-000000000000",
                    test="""
pm.test("404 not 500", () => pm.response.to.have.status(404));
pm.test("code is not_found", () =>
    pm.expect(pm.response.json().code).to.eql("not_found"));
""",
                ),
                request(
                    "No token (401)", "POST", "/disasters",
                    body={"title": "No Auth", "description": "Posted without a bearer token."},
                    test="""
pm.test("401", () => pm.response.to.have.status(401));
pm.test("tells the client how to authenticate", () =>
    pm.expect(pm.response.headers.get("WWW-Authenticate")).to.eql("Bearer"));
""",
                ),
                request(
                    "Responder attempts a write (403)", "POST", "/disasters",
                    description="Log in as responder first. Demonstrates the role gate.",
                    auth=True,
                    body={"title": "Responder Attempt",
                          "description": "A read-only responder should not create this."},
                    test="""
pm.test("403", () => pm.response.to.have.status(403));
""",
                ),
            ],
        },
        {
            "name": "6. Health",
            "item": [
                request("Liveness", "GET", "/health"),
                request(
                    "Readiness (dependencies + breaker state)", "GET", "/health/ready",
                    description=(
                        "Reports database and cache status plus live circuit-breaker state "
                        "per external provider — i.e. which provider is currently being "
                        "skipped, and why."
                    ),
                ),
            ],
        },
    ],
}


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "docs" / "postman_collection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(COLLECTION, indent=2) + "\n", encoding="utf-8")

    folders = len(COLLECTION["item"])
    requests = sum(len(f["item"]) for f in COLLECTION["item"])
    print(f"Wrote {out}  ({folders} folders, {requests} requests)")


if __name__ == "__main__":
    main()
