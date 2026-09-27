# Disaster Response Coordination Platform

A backend for coordinating disaster response: incident records, automatic location
resolution from free-text descriptions, geospatial resource search, aggregated
community reports from external providers, and a live event stream.

**Python 3.12 · FastAPI · PostgreSQL 16 + PostGIS 3.4 · Redis 7 · SQLAlchemy 2.0 (async)**

No API keys are required. `docker compose up` gives you a fully working system.

---

## Contents

- [Quick start](#quick-start)
- [What to look at first](#what-to-look-at-first)
- [Architecture](#architecture)
- [Data model](#data-model)
- [API reference](#api-reference)
- [Technical decisions](#technical-decisions)
  - [Why PostgreSQL + PostGIS](#why-postgresql--postgis)
  - [How the geospatial search works](#how-the-geospatial-search-works)
  - [How location resolution works](#how-location-resolution-works)
  - [How caching works](#how-caching-works)
  - [How external API failures are handled](#how-external-api-failures-are-handled)
  - [Real-time updates](#real-time-updates)
  - [Authentication and authorization](#authentication-and-authorization)
- [Testing](#testing)
- [Assumptions](#assumptions)
- [Trade-offs](#trade-offs)
- [Known limitations and future work](#known-limitations-and-future-work)
- [AI-assisted development](#ai-assisted-development)

---

## Quick start

**Prerequisites:** Docker and Docker Compose. Nothing else — no Python install, no
API keys.

```bash
git clone <repository-url>
cd disaster-response-platform

cp .env.example .env          # defaults are local-only and safe to use as-is

docker compose up -d --build  # postgres+postgis, redis, api, worker, mock providers
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed
```

That gives you:

| URL | What |
|---|---|
| http://localhost:8000/docs | Swagger UI (click **Authorize** after logging in) |
| http://localhost:8000/demo/ | Interactive map with a live event feed |
| http://localhost:8000/redoc | ReDoc API reference |
| http://localhost:8000/api/v1/health/ready | Dependency health |

Seeded accounts:

| username | password | role | can do |
|---|---|---|---|
| `admin` | `admin123` | admin | everything, including `DELETE` |
| `contributor` | `contributor123` | contributor | create; update **only their own** records |
| `contributor2` | `contributor123` | contributor | (second account, for the ownership demo) |
| `responder` | `responder123` | responder | read-only |

On macOS/Linux a `Makefile` wraps these: `make up && make migrate && make seed && make test`.
Windows has no `make`, so the raw `docker compose` commands above are the primary path.

### Running the tests

```bash
docker compose exec api pytest              # 108 tests
docker compose exec api pytest --cov=app    # with coverage
```

Tests use a separate `disaster_response_test` database (created automatically on
first container start) and never touch development data or the network.

---

## What to look at first

Five commands that demonstrate the parts of the brief that carry the most weight.

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

**1. Location resolution from free text** — no coordinates supplied:

```bash
curl -s -X POST localhost:8000/api/v1/disasters \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"title":"Manhattan Flooding",
       "description":"Heavy flooding has affected Manhattan, NYC. Subway stations are submerged.",
       "tags":["flood"]}'
```

```jsonc
"location": {
  "name": "Manhattan, New York, USA",
  "point": { "latitude": 40.7831, "longitude": -73.9712 },
  "source": "rules:cue_known+gazetteer",   // how we got here
  "confidence": 0.63                        // extraction 0.90 x geocoder 0.70
}
```

**2. Geospatial search** (PostGIS `ST_DWithin`, GiST index, distances from the database):

```bash
curl -s "localhost:8000/api/v1/disasters/$ID/resources?radius_km=5"
```

```
 0.449 km  rescue    Central Park Staging Area
 3.313 km  shelter   Midtown Relief Shelter
 3.790 km  food      Harlem Community Kitchen
 4.893 km  hospital  Bellevue Hospital Center
```

**3. Caching** — watch the `X-Cache` header on two consecutive calls:

```bash
curl -sD- -o /dev/null "localhost:8000/api/v1/disasters/$ID/reports" | grep -i x-cache  # MISS
curl -sD- -o /dev/null "localhost:8000/api/v1/disasters/$ID/reports" | grep -i x-cache  # HIT
```

**4. Graceful degradation** — kill the external providers outright:

```bash
docker compose stop mock-services
curl -s "localhost:8000/api/v1/disasters/$ID/reports" | python -m json.tool | head -20
```

Returns **HTTP 200** with the last known reports, plus honest metadata:

```jsonc
"meta": {
  "cache": "STALE",
  "degraded": true,
  "sources": { "mock-social-api": "failed", "mock-official-api": "failed" }
}
```

Disaster CRUD is entirely unaffected. `docker compose start mock-services` to restore.

**5. Real-time** — in one terminal:

```bash
curl -N localhost:8000/api/v1/events/stream
```

then create or update a disaster in another, and watch `disaster_created` /
`disaster_updated` arrive immediately.

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
  /demo/  (Leaflet) │  FastAPI  (uvicorn, ASGI)                    │
  curl -N /events   │                                              │
  Swagger /docs     │  middleware: request-id → rate-limit →       │
                    │              CORS → security headers         │
                    │     │                                        │
                    │     ▼                                        │
                    │  api/v1/routes/    HTTP + Pydantic only      │
                    │     │              role gates as deps        │
                    │     ▼                                        │
                    │  services/         business rules, ownership │
                    │     │              checks, transactions      │
                    │     ├───────────────────┬──────────────┐     │
                    │     ▼                   ▼              ▼     │
                    │  repositories/       cache/       realtime/  │
                    │  (SQL, PostGIS)    (L1 in-proc)   (pub/sub)  │
                    └─────┬───────────────────┬──────────────┬─────┘
                          │                   │              │
                  ┌───────▼───────┐   ┌───────▼───────┐      │
                  │ PostgreSQL 16 │   │    Redis 7    │◄─────┘
                  │   + PostGIS   │   │ cache · pubsub│
                  │  GiST + GIN   │   │   · job queue │
                  └───────────────┘   └───────┬───────┘
                                              │
                            ┌─────────────────┴──────────┐
                            │  arq worker (optional)      │
                            │  pre-warms report caches    │
                            └─────────────────────────────┘
                                              │
                    ┌─────────────────────────▼─────────────────────┐
                    │ integrations/  httpx + timeout + retry +      │
                    │                circuit breaker + rate limiter │
                    └──┬──────────────────┬─────────────────────┬───┘
                       ▼                  ▼                     ▼
              mock-services:9000     Nominatim (OSM,      Anthropic API
              social + official      no key required)     (optional)
              (separate container)
```

**Layering rule, enforced by convention and visible in the imports:**

| Layer | May do | May not do |
|---|---|---|
| `api/v1/routes` | parse/validate HTTP, check roles, shape responses | touch the database, contain business rules |
| `services` | business rules, ownership checks, orchestration, transactions | build SQL, know about HTTP |
| `repositories` | SQL and PostGIS | know about HTTP or permissions |
| `integrations` | outbound calls, normalisation, resilience | know about the database |

The payoff: business rules are testable without a web server, and the geospatial
SQL is readable without wading through permission logic.

### Project layout

```
app/
├── main.py              application factory, lifespan, middleware wiring
├── core/                config, logging, errors, security, deps, middleware
├── api/v1/routes/       auth · disasters · resources · reports · events · health
├── schemas/             Pydantic request/response models (the API contract)
├── db/                  engine/session + SQLAlchemy models
├── repositories/        data access; geo.py holds the PostGIS helpers
├── services/            business logic; classification.py is the priority scorer
├── integrations/        http, resilience, nominatim, llm, gazetteer, providers
├── cache/               backend abstraction + two-tier cache with SWR
├── realtime/            event bus over Redis pub/sub
└── workers/             arq background jobs (optional)

mock_services/main.py    stand-in social + official providers (own container)
migrations/              Alembic; 0001 creates extensions, tables, GiST/GIN indexes
scripts/                 seed, export_openapi, demo_events
tests/                   108 tests
web/index.html           the demo map client
docs/                    architecture.md, openapi.json, postman_collection.json
```

---

## Data model

```sql
users      (id, username UNIQUE, password_hash, role, is_active, timestamps)

disasters  (id, title, description,
            location_name     TEXT,                     -- "Manhattan, NYC"
            location          GEOGRAPHY(POINT,4326),    -- nullable, see below
            location_source   TEXT,                     -- provenance
            location_confidence NUMERIC(3,2),
            tags              TEXT[] NOT NULL DEFAULT '{}',
            status            disaster_status,
            created_by        UUID REFERENCES users ON DELETE RESTRICT,
            timestamps)

resources  (id, name, type resource_type,
            location GEOGRAPHY(POINT,4326) NOT NULL,    -- required here
            address, capacity, contact, is_active,
            disaster_id UUID NULL REFERENCES disasters ON DELETE SET NULL,
            timestamps)

audit_log  (id, entity_type, entity_id, action, actor_id, actor_username,
            changes JSONB, request_id, created_at)      -- append-only
```

Indexes:

| Index | Type | Why |
|---|---|---|
| `ix_resources_location` | **GiST** | makes `ST_DWithin` an index scan — see below |
| `ix_disasters_location` | **GiST** | same, for proximity-filtered disaster lists |
| `ix_disasters_tags` | **GIN** | `tags @> ARRAY['flood']` becomes an index lookup |
| `ix_disasters_title_trgm` | **GIN (pg_trgm)** | `?q=` uses a leading wildcard, which btree cannot serve |
| `ix_disasters_status_created_at` | btree composite | the common list query filters then sorts, satisfied from one index |

Decisions worth stating:

- **`disasters.location` is nullable, `resources.location` is not.** Location on an
  incident is *inferred* from free text and can legitimately fail; rejecting a
  report filed under pressure because a geocoder was down would be the wrong
  behaviour. A resource with no coordinates, by contrast, can never be returned
  by a proximity search, so it has no reason to exist.
- **`tags` is `TEXT[]` + GIN, not a join table.** Tags are a small, closed,
  read-mostly vocabulary queried only by containment. A join table would add two
  joins to every list query for referential integrity nobody needs. If tags ever
  gained attributes of their own, that calculus flips.
- **Location provenance is stored, not just coordinates.** `location_source` and
  `location_confidence` travel with every point, because an operator dispatching
  a rescue team needs to know the difference between "a rule matched a place
  dictionary" and "a human typed this in".
- **UUID primary keys.** Ids appear in URLs and are broadcast over WebSocket; a
  sequential integer would leak how many incidents exist and invite enumeration.
- **`audit_log` has no FK to `users`.** A trail must outlive the rows it
  describes: `RESTRICT` would block user deletion forever, `CASCADE` would
  destroy the evidence you kept it for.

---

## API reference

Full interactive docs at `/docs`; the generated spec is committed at
[`docs/openapi.json`](docs/openapi.json) and a Postman collection at
[`docs/postman_collection.json`](docs/postman_collection.json).

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/v1/auth/login` | — | returns a JWT |
| `GET` | `/api/v1/auth/me` | any | current token holder |
| `POST` | `/api/v1/disasters` | admin, contributor | resolves location from `description` |
| `GET` | `/api/v1/disasters` | — | `?tag= &status= &q= &created_by= &lat= &lng= &radius_km= &sort= &limit= &offset=` |
| `GET` | `/api/v1/disasters/{id}` | — | |
| `PATCH` | `/api/v1/disasters/{id}` | admin **or owner** | partial update |
| `DELETE` | `/api/v1/disasters/{id}` | **admin only** | 204 |
| `GET` | `/api/v1/disasters/{id}/resources` | — | `?lat= &lng= &radius_km= &type= &limit=` |
| `GET` | `/api/v1/disasters/{id}/reports` | — | `?priority= &limit= &refresh=` |
| `GET` | `/api/v1/disasters/{id}/audit` | any | change history |
| `GET` | `/api/v1/resources` | — | `?type= &disaster_id=` |
| `POST` | `/api/v1/resources` | **admin only** | |
| `GET` | `/api/v1/events/stream` | — | Server-Sent Events |
| `WS` | `/api/v1/events/ws` | — | WebSocket |
| `GET` | `/api/v1/health`, `/health/ready` | — | liveness / readiness |

### Response conventions

List endpoints return an envelope, never a bare array — there would otherwise be
nowhere to put a total or a degraded flag without a breaking change:

```jsonc
{ "items": [ ... ], "meta": { "total": 42, "limit": 20, "offset": 0, "has_more": true } }
```

Every error is an RFC 9457 problem document served as `application/problem+json`:

```jsonc
{
  "type": "https://docs.drp.local/errors/forbidden",
  "title": "Not permitted",
  "status": 403,
  "detail": "You may only modify disasters you created.",
  "instance": "/api/v1/disasters/7c9e...",
  "code": "forbidden",
  "request_id": "018f3c2e-9b1a-7c3d-8e4f-1a2b3c4d5e6f"
}
```

One envelope means a client needs one parser, and `request_id` (also returned as
`X-Request-ID` on every response) ties any reported failure straight to a log line.

Useful response headers: `X-Request-ID`, `X-Cache`, `X-Cache-Age`, `X-Degraded`,
`X-RateLimit-Remaining`, `X-Response-Time-ms`.

---

## Technical decisions

### Why PostgreSQL + PostGIS

The deciding requirement was geospatial search. PostGIS gives a **real spatial
index** (GiST) and spheroid-accurate distances in metres; the alternatives all
cost something that mattered here:

| Option | Why not |
|---|---|
| SQLite + Haversine in SQL | No spatial index. Every query is a full scan, and the arithmetic breaks near the poles and the antimeridian. |
| MongoDB `2dsphere` | Genuinely good geo support, but this data is highly relational (incidents → users → resources, plus an audit trail) and I wanted real foreign keys and transactions. |
| Postgres without PostGIS | Possible with a bounding-box prefilter plus Haversine, but I would be reimplementing — worse — what PostGIS already does. |

PostgreSQL also happened to cover everything else in one system: `TEXT[]` + GIN
for tags, `pg_trgm` for the search box, `JSONB` for audit diffs, enums for closed
vocabularies, and transactions so an incident and its audit row commit together.

### How the geospatial search works

`GET /disasters/{id}/resources?lat=&lng=&radius_km=` compiles to:

```sql
SELECT r.*,
       ST_Distance(r.location, $point) / 1000.0 AS distance_km
FROM resources r
WHERE r.is_active
  AND ST_DWithin(r.location, $point, $radius_km * 1000)   -- uses the GiST index
ORDER BY ST_Distance(r.location, $point)
LIMIT $limit;
```

where `$point = ST_SetSRID(ST_MakePoint($lng, $lat), 4326)::geography`.

Four things are load-bearing:

**1. `geography`, not `geometry`.** With `geometry` at SRID 4326, `ST_Distance`
returns *degrees*, which are not a distance — one degree of longitude is 111 km
at the equator and 0 km at the pole. Getting real metres out of `geometry` means
projecting to a local CRS (UTM), which means picking the right zone per query.
`geography` does the spheroid maths directly and returns metres.

**2. `ST_DWithin`, not `ST_Distance(...) < r`.** They return identical results and
have completely different query plans. `ST_DWithin` is rewritten by the planner
into a bounding-box overlap (`&&`) the GiST index can answer, then computes exact
distances only on the survivors. `ST_Distance(...) < r` must be evaluated for
every row before it can be compared, which forces a sequential scan.

Measured on this schema with 200,000 resources (`EXPLAIN (ANALYZE, BUFFERS)`,
full plans in [`docs/architecture.md`](docs/architecture.md)):

| Query | Plan | Buffers | Time |
|---|---|---|---|
| `ST_DWithin(...)` | **Bitmap Index Scan** on `ix_resources_location` | 257 | **6.47 ms** |
| `ST_Distance(...) < r` | **Parallel Seq Scan** | 3,948 | **311.92 ms** |

**~48× faster and 15× fewer buffer reads for the same answer.**

**3. Distance is computed and sorted in SQL.** That way `ORDER BY distance LIMIT 20`
genuinely returns the twenty nearest rows, instead of fetching every match and
sorting in Python.

**4. Longitude comes first.** `ST_MakePoint` is `(x, y)` — that is `(lng, lat)`.
Passing them the other way round produces a point that is perfectly valid and
completely wrong. The swap happens in exactly one place
(`app/repositories/geo.py::geography_point`), which takes named `lat`/`lng`
arguments so no call site can get it wrong.

There is a regression test for the antimeridian
(`tests/test_geospatial.py::test_antimeridian_is_handled_correctly`): two points
either side of 180° must be ~112 km apart, not ~19,000 km. That is the case that
breaks flat-plane distance maths.

`radius_km` is capped at `MAX_RADIUS_KM` (default 500). An unbounded radius would
let one request scan the whole table and defeat the index.

### How location resolution works

```
"Heavy flooding has affected Manhattan, NYC."
                 │
   STEP 1  extract a place name
           LLM (only if ANTHROPIC_API_KEY set) ──fails──► rule-based extractor
                 │                                         (cue patterns + gazetteer)
                 ▼
           "Manhattan, NYC"   (confidence 0.90)
                 │
   STEP 2  resolve to coordinates
           cache ──miss──► Nominatim (no key) ──fails/no match──► bundled gazetteer
                 │                                                  (confidence 0.70)
                 ▼
           (40.7831, -73.9712)  ·  source "rules:cue_known+gazetteer"  ·  conf 0.63
```

**Extraction and geocoding are separate steps, deliberately.** It is tempting to
ask an LLM for coordinates directly. That is a bad idea: a model will happily
return a confident, plausible, wrong lat/lng and nothing downstream can detect
it. So the model is only ever asked for a *name*, and a real geocoder turns names
into points. Each component does the job it can be held accountable for.

**The rule-based extractor** matches locative cues (`in`, `near`, `at`,
`affected`, `struck`, …) followed by capitalised runs, scored against a bundled
gazetteer of ~150 places:

| Confidence | Method | Meaning |
|---|---|---|
| 0.90 | `rules:cue_known` | known place after a locative cue |
| 0.75 | `rules:known` | known place anywhere in the text |
| 0.55 | `rules:cue` | unknown proper noun after a cue (still sent to the geocoder) |
| 0.35 | `rules:proper_noun` | a capitalised run and nothing more |

It is rule-based rather than a model because the text here is short and
formulaic, and because the result is deterministic, instant, free, and explains
itself. The LLM path handles messier input when a key exists; the rules are the
floor, not the ceiling.

**Confidence is multiplicative** — a 0.90 extraction feeding a 0.70 geocoder is a
0.63 result. Uncertainty compounds and the number should say so.

**Resolution failure is never fatal.** If nothing resolves, the incident is still
created with `location.point = null` and `source = "unresolved"`.

> **Note on Nominatim.** It needs no API key, but it rate-limits to 1 req/s and
> blocks clients it considers abusive. From inside Docker it frequently returns
> `403 Access denied`. **This is handled, not a bug** — you will see
> `geocoder_unavailable_using_gazetteer` in the logs and everything still
> resolves via the offline gazetteer. It is an accurate live demonstration of
> the fallback chain. Set `GEOCODER_ENABLED=false` to skip the attempt entirely.

### How caching works

```
request
   │
   ▼
L1  in-process dict (CACHE_L1_TTL, 30s)  ──hit──► return        X-Cache: HIT
   │ miss
   ▼
L2  Redis envelope {value, fetched_at, ttl}
   │   age < ttl?  ──yes──► return                              X-Cache: HIT
   │ no (miss, or soft-expired)
   ▼
single-flight lock  (Redis SET NX EX — exactly one winner)
   │ won                              │ lost
   ▼                                  ▼
call the origin                   serve stale if available,
   │ ok        │ failed           else wait briefly for the winner
   ▼           ▼
store, return  serve stale        X-Cache: STALE, degraded: true
               (or 502 if nothing cached at all)
```

Three ideas do the real work:

**Soft vs hard expiry.** The stored envelope records when it was fetched. `ttl` is
the *soft* expiry — past it the value is stale but still usable. The Redis key
itself lives for `ttl + stale_grace`. So when an origin is down we answer from
stale data flagged `degraded: true` instead of returning 502. For disaster
reports, five-minute-old data is enormously more useful than an error page.

**Single-flight.** On a cold key, N concurrent requests would otherwise all call
the origin at once — a cache stampede, arriving exactly when the origin is least
able to cope. One caller wins an atomic `SET NX` lock and fetches; the rest serve
stale or wait briefly. Tested with 20 concurrent callers asserting **one** origin
call (`test_single_flight_collapses_a_concurrent_stampede`).

**L1 is not about Redis latency.** It collapses the burst of identical calls a
single page render produces into one Redis round-trip. Being per-worker and
short-lived bounds cross-worker inconsistency to `CACHE_L1_TTL`.

TTLs, and why:

| Namespace | TTL | Reasoning |
|---|---|---|
| `geocode:{place}` | 7 days | A city does not move. This is also what keeps us inside Nominatim's 1 req/s limit. |
| `nlp:place:{hash}` | 24 h | Deterministic for a given description; the TTL only bounds prompt changes. |
| `reports:{id}` | 5 min | Social chatter must stay reasonably fresh. |
| `official:{id}` | 15 min | Official bulletins change slowly. |
| *(degraded result)* | 60 s | A partial result is cached only briefly so a recovering provider is picked up fast. |
| stale grace | 1 h | How long stale data may be served when the origin is down. |

**Invalidation is targeted, never a flush.** Editing a disaster's description or
location drops that disaster's report cache (the provider query is derived from
those fields), but leaves the geocode cache alone — its entries cost a
rate-limited external call to rebuild.

### How external API failures are handled

Three different failures need three different tools, and conflating them is the
usual mistake:

| Tool | Handles | Why not the others |
|---|---|---|
| **Retry** (exp. backoff + full jitter) | a transient blip: one dropped packet, one 503 mid-deploy | useless against a sustained outage |
| **Circuit breaker** | a sustained outage | retrying a dead service burns our latency budget and keeps hammering something trying to recover |
| **Rate limiter** (token bucket) | a published quota (Nominatim: 1 req/s) | nothing is broken; we simply must not exceed it |

**Jitter matters.** Without it, every client that failed at the same moment
retries at the same moment, reconverging into a synchronised thundering herd.
Backoff is randomised across the whole window.

**Only 5xx, timeouts and 429 are retried.** A 400 or 422 will fail identically
every time — retrying it just multiplies the latency of a request that was always
going to fail. `http.py` classifies every response into `TransientError` or
`PermanentError`, and only the former is retried or counted against the breaker.

**Breaker state machine** (per provider, per process):

```
CLOSED  ──5 consecutive failures──►  OPEN
OPEN    ──30s elapsed────────────►  HALF_OPEN     (admits exactly one trial call)
HALF_OPEN ──trial succeeds───────►  CLOSED
HALF_OPEN ──trial fails──────────►  OPEN          (timer restarts)
```

Half-open admits *one* probe: letting full load through the instant the timer
expires would re-flatten a service that is still fragile. The counter resets on
any success, so isolated blips never accumulate toward tripping.

**The fallback chain**, in order: fresh cache → stale cache → bundled
fixture/gazetteer → empty result with `degraded: true`. Only a cold cache *and* a
dead upstream produces a 502.

**Partial failure is a first-class outcome.** The two providers are fetched
concurrently with `asyncio.gather(..., return_exceptions=True)` — the
`return_exceptions` part is essential, because without it one failure cancels the
sibling task and a *working* provider's data is discarded. One provider down
still returns the other's reports with `degraded: true` and a per-provider
`sources` map (`ok` / `empty` / `failed` / `circuit_open`).

When stale data is served, `sources` reports why each provider failed **on this
request**, read from the live breaker state — not the provenance stored with the
cached payload, which would typically say `ok` and be actively misleading.

**Blast radius is contained.** These calls happen only behind the reports
endpoint. A dead social API degrades that one feed and cannot affect disaster
CRUD — asserted by `test_a_failing_provider_never_breaks_disaster_crud`.

The mock providers run as a **separate container on their own port**, not as an
in-process stub, so timeouts, retries and the breaker are exercised against a
real socket that can really be slow, really 503, and really be stopped mid-demo.
They expose a chaos knob:

```bash
curl -X POST "localhost:9000/_control/chaos?failure_rate=0.8&latency_ms=3000"
curl -X POST "localhost:9000/_control/chaos?failure_rate=0.0"     # restore
```

### Real-time updates

Events are published to a Redis pub/sub channel and fanned out to **both** a
WebSocket and an SSE endpoint. Two transports because they suit different
consumers: WebSocket is bidirectional and drives the browser map; SSE is plain
HTTP, needs no client library, survives proxies that mishandle upgrades, and can
be verified with a single `curl -N`.

Emitted: `disaster_created`, `disaster_updated` (with `changed_fields`),
`disaster_deleted`, `resource_created`.

```jsonc
{ "id": "…", "event": "disaster_updated", "ts": "2026-09-27T…Z", "data": { … } }
```

**Why pub/sub rather than a process-local list of connections.** With more than
one uvicorn worker, a connection lives in exactly one process. An in-process
broadcast would reach only the clients that happened to land on the worker
handling the write; everyone else silently misses the event. Redis pub/sub means
any worker can publish and every worker delivers.

**Events are published after commit**, never before — announcing a change that
can still roll back would let a client `GET` the resource and receive a 404.

**Delivery is best-effort and at-most-once.** No replay buffer, no
acknowledgement: a client that disconnects misses what happened and must re-fetch
on reconnect. That is the right trade for live situational awareness — the REST
API remains the source of truth; this only says that it changed.

### Authentication and authorization

JWT (HS256) with Argon2id password hashes. HS256 because a single service both
issues and verifies; the moment a second, independently-deployed verifier
existed, RS256/EdDSA would be correct instead. Argon2id because it is memory-hard,
so a GPU attacker gains far less than with bcrypt.

Authorization is split across two layers **on purpose**:

**Role gates** live in route dependencies — "only an admin may `DELETE`" is a
property of the endpoint, decidable before any data is loaded, and it shows up in
the OpenAPI document.

**Ownership checks** live in the service layer — "a contributor may edit only
their own incident" cannot be decided until the record exists.

The demonstrated rule:

| Actor | `PATCH` own | `PATCH` someone else's | `DELETE` |
|---|---|---|---|
| admin | 200 | 200 | 204 |
| contributor | **200** | **403** | 403 |
| responder | 403 | 403 | 403 |

Both contributors hold the *same role* and both pass the route's role gate — only
a record-level check can tell them apart. That distinction is the whole point of
the split, and it is covered by
`tests/test_authz_rules.py::test_contributor_cannot_update_someone_elses_disaster`,
which also asserts the record was genuinely left unchanged.

Other security measures: the request pipeline **re-reads the user from the
database** rather than trusting the role claim in the token, so deactivation
takes effect immediately; login answers identically for "no such user" and "wrong
password" (and verifies against a dummy hash when the user is missing, so
response timing does not leak account existence either); tighter rate limiting on
`/auth/login` than the rest of the API; `algorithms` pinned to one value when
decoding, which is how the classic `alg: none` and HS/RS confusion attacks are
prevented; and the unhandled-exception handler never echoes exception text to the
caller.

---

## Testing

108 tests, all offline and deterministic. Every external HTTP call is intercepted
by `httpx.MockTransport`.

```
tests/test_disasters_api.py          API endpoints: create/read/list/patch, filtering, pagination, audit
tests/test_validation_errors.py      validation + error contract: 422/404/401 shapes, blank input, bad enums
tests/test_authz_rules.py            the business rule: ownership, role gates, deactivation, forged tokens
tests/test_geospatial.py             radius boundaries, distance accuracy, ordering, antimeridian
tests/test_cache_and_resilience.py   cache hit/miss/stale, single-flight, retry, breaker, rate limiter
tests/test_reports_integration.py    external mock: normalisation, caching, partial + total failure
tests/test_classification.py         priority scorer + place extraction
```

Mapped to what the brief asked for:

| Required | Where |
|---|---|
| One API endpoint | `test_create_disaster_resolves_location_from_description`, `test_list_disasters_filters_by_tag` |
| One validation/error scenario | `test_missing_required_fields_returns_422_problem_document`, `test_unknown_disaster_returns_404_not_500` |
| One important business rule | `test_contributor_cannot_update_someone_elses_disaster`, `test_only_admin_may_delete` |
| One external integration/mock | `test_provider_payloads_are_normalised_into_one_shape`, `test_second_request_is_a_cache_hit_and_does_not_call_the_provider` |

Two things worth highlighting about the approach:

**Cache tests count upstream calls.** Asserting `X-Cache: HIT` alone proves
nothing — the header could simply be wrong. The tests count invocations of the
mock transport, so a cache hit is proven to have avoided the network.

**Geospatial tests run against real PostGIS.** The claim under test is that
`ST_DWithin` returns the right rows with the right distances; mocking the
database would test the mock. Isolation comes from running each test inside a
transaction that is rolled back (`join_transaction_mode="create_savepoint"`, so
the application's own `commit()` calls are genuinely exercised).

---

## Assumptions

1. **Mock users are sufficient.** The brief says so explicitly. There is no
   registration, password reset, or refresh-token flow.
2. **Reports are not persisted.** They are external, ephemeral, and cached — so
   they are normalised and cached, not stored. Persisting them would add a
   sync/dedup problem the brief does not ask for.
3. **Resource data is reference data**, maintained by admins rather than
   crowd-sourced, so resource writes are admin-only while incident writes are not.
4. **One incident has one location.** Real events (a cyclone track, a river
   flood) are lines or polygons. PostGIS handles those natively, and the schema
   could move to `GEOGRAPHY(GEOMETRY)` without changing the query shape — but the
   brief asks for latitude and longitude.
5. **Approximate city-level coordinates are acceptable.** The gazetteer stores
   centroids, which is adequate for radius search at city scale; the confidence
   score communicates this.
6. **Reads are public.** Only writes are authenticated, so the map demo renders
   without credentials. A real deployment would likely authenticate reads too.
7. **Single-region, moderate scale.** Offset pagination and per-process circuit
   breakers are fine at this size; both are called out below.

---

## Trade-offs

Things deliberately simplified, and what I would do instead with more time.

| Decision | Trade-off | Alternative |
|---|---|---|
| **Offset pagination** | Simple, supports arbitrary sorts and random page access; degrades on deep pages because the database still walks skipped rows | Keyset/cursor pagination |
| **Hard delete** | Simple and predictable; history survives only in `audit_log` | Soft delete — but it infects every query with `WHERE deleted_at IS NULL`, so half-doing it is worse than not doing it |
| **Rule-based classifier** | Deterministic, explainable, instant, free; cannot read negation ("we are NOT trapped"), sarcasm, or other languages | Rules as a pre-filter plus a trained classifier behind them |
| **Per-process circuit breakers** | No coordination needed; each worker independently stops calling a dead dependency, so N workers each spend N failures learning the same fact | Shared state in Redis |
| **Fixed-window rate limiting** | One atomic `INCR` per request; tolerates up to 2× the limit across a window boundary | Sliding window or token bucket |
| **ILIKE + trigram for `?q=`** | Good enough for an operator search box | `tsvector` + `ts_rank` for real relevance ranking |
| **JWT with no revocation** | Stateless, no session store; a stolen token is valid until it expires | Short access tokens + refresh tokens + a denylist (`jti` is already in the claims for this) |
| **In-memory cache fallback** | The app still boots without Redis; the cache stops being shared and events stop crossing workers | Set `CACHE_ALLOW_MEMORY_FALLBACK=false` and let the health check pull the instance from rotation |
| **No CI pipeline** | Out of scope for the time budget | GitHub Actions running `ruff` + `pytest` against a PostGIS service container |

---

## Known limitations and future work

- **No pagination on the reports feed.** It is capped at `limit=200` from a
  bounded number of providers, so it cannot grow unboundedly, but it is not paged.
- **The worker is at-least-once with no deduplication.** Harmless here, because
  the only job is an idempotent cache refresh, but any job with side effects
  would need an idempotency key.
- **WebSocket connections are unauthenticated.** Acceptable while events carry
  only summary data that is already readable through the public list endpoint;
  a private deployment would need a token on the upgrade request.
- **No observability beyond structured logs.** Prometheus metrics (cache hit
  ratio, breaker state, upstream latency histograms) and OpenTelemetry tracing
  are the obvious next step; `request_id` already threads through everything,
  so traces would slot in cleanly.
- **Gazetteer coverage is ~150 places.** It is a fallback, not a geocoder. With a
  key configured, or with Nominatim reachable, coverage is global.
- **No clustering on the demo map.** Fine at 33 resources; it would need marker
  clustering at thousands.
- **Load testing has not been done.** The `EXPLAIN ANALYZE` numbers above are
  single-query measurements, not a concurrency benchmark.

---

## Configuration

Every tunable is in `.env.example` with an explanatory comment, read once at
startup into a frozen settings object (`app/core/config.py`) — nothing else in the
codebase reads `os.environ`, so there is one honest answer to "where does this
value come from?".

The committed defaults are local-development values and contain no real secrets.
**`.env` is gitignored.** Before any real deployment, set `JWT_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The application logs a loud error if it starts in `production` with the default
secret still in place.

---

## AI-assisted development

**Tool used:** Claude (Anthropic), via Claude Code, as a pair-programming assistant.

**What it helped with**

- Scaffolding boilerplate: the Alembic migration skeleton, Pydantic model
  definitions, the Docker/Compose setup, and the repetitive parts of the test
  suite.
- Drafting the bundled gazetteer data and the mock providers' sample content.
- Producing first drafts of docstrings and this README from the decisions I had
  already made.
- Acting as a reviewer — talking through where the ownership check belonged, and
  whether the circuit breaker should wrap the retry loop or sit inside it.

**What I designed, decided and own**

- The overall architecture and the layering rule (routes → services →
  repositories), including the deliberate split of authorization across the route
  layer (roles) and the service layer (ownership).
- Every database decision: `geography` over `geometry`, nullable incident
  location versus non-null resource location, `TEXT[]` + GIN over a tag join
  table, storing resolution provenance, and the choice of each index.
- The caching strategy: two tiers, soft/hard expiry for stale-while-revalidate,
  single-flight locking, per-namespace TTLs, and targeted invalidation.
- The resilience design: which errors are transient versus permanent, retry only
  on transient, the breaker's half-open single-probe behaviour, and the ordering
  of breaker around retry so a run of retries counts as one failure.
- The decision to keep the LLM away from coordinates and restrict it to name
  extraction.

**What I reviewed, corrected and validated**

I read and ran everything. Bugs I found and fixed during development, which are
worth listing because they are the substance of the review:

1. The place extractor ran past sentence boundaries — `"...a warehouse in
   Brooklyn. Smoke is drifting..."` produced the candidate `"Brooklyn. Smoke"`,
   which then failed to geocode. Fixed with an abbreviation-aware split so
   `"St. Louis"` still survives. Regression tests added for both halves.
2. The priority classifier double-counted overlapping signals: `"baby formula"`
   matched the urgent-need lexicon *and* the vulnerable-group regex on the same
   word, inflating ordinary supply requests into the `critical` bucket. Fixed by
   tracking matched character spans.
3. `os.environ.setdefault` in the test harness was a silent no-op under Docker
   (Compose already injects `APP_ENV`), so the suite ran against the
   **development** database and dropped its tables. Changed to explicit
   assignment.
4. Rate-limit rejections raised from middleware escaped the registered exception
   handlers — those live inside the routing layer — and surfaced as bare 500s.
   The problem document is now constructed directly in the middleware.
5. `meta.sources` on a stale response echoed the *cached* payload's provenance
   (typically `ok, ok`) instead of why the live fetch had just failed. Now read
   from live circuit-breaker state.
6. A strict `default-src 'none'` CSP was being applied to the demo page, blocking
   Leaflet; the asset-serving paths are now exempted rather than the policy
   weakened everywhere.
7. `pyproject.toml` was baked into the image but not mounted, so pytest
   configuration changes appeared to do nothing. Added to the dev mounts.

I also ran the `EXPLAIN ANALYZE` benchmark myself to confirm the `ST_DWithin`
claim with real numbers rather than asserting it from memory, and verified the
degradation path by actually stopping the provider container.

I am able to explain and defend every file in this repository.

---

## License

MIT
