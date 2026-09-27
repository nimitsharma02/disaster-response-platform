# Disaster Response Coordination Platform

A backend for coordinating disaster response. It stores incidents, works out
where they are from a free text description, finds resources near them, pulls in
community reports from external providers, and pushes live updates to connected
clients.

Built with Python 3.12, FastAPI, PostgreSQL 16 with PostGIS, Redis and
SQLAlchemy 2.0 async.

No API keys are needed. `docker compose up` gives you a working system.

---

## Setup

You need Docker and Docker Compose. Nothing else.

```bash
git clone <repository-url>
cd disaster-response-platform

cp .env.example .env

docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed
```

Once that finishes:

- Swagger UI at http://localhost:8000/docs
- Demo map with live updates at http://localhost:8000/demo/
- ReDoc at http://localhost:8000/redoc
- Health check at http://localhost:8000/api/v1/health/ready

Seeded accounts:

- `admin` / `admin123` gives full access, including delete
- `contributor` / `contributor123` can create, and update only their own records
- `contributor2` / `contributor123` is a second account, used to show the ownership rule
- `responder` / `responder123` is read only

There is a Makefile for Linux and macOS (`make up`, `make migrate`, `make seed`,
`make test`). Windows has no make, so the docker compose commands above are the
main path.

### Running the tests

```bash
docker compose exec api pytest
docker compose exec api pytest --cov=app
```

108 tests. They use a separate `disaster_response_test` database, created
automatically when the database container first starts, so they never touch your
development data. No network calls either.

---

## Quick tour

Get a token first:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"admin123"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

Create an incident without giving coordinates:

```bash
curl -s -X POST localhost:8000/api/v1/disasters \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"title":"Manhattan Flooding",
       "description":"Heavy flooding has affected Manhattan, NYC. Subway stations are submerged.",
       "tags":["flood"]}'
```

The response says where the location came from and how confident it is:

```json
"location": {
  "name": "Manhattan, New York, USA",
  "point": { "latitude": 40.7831, "longitude": -73.9712 },
  "source": "rules:cue_known+gazetteer",
  "confidence": 0.63
}
```

Find resources nearby:

```bash
curl -s "localhost:8000/api/v1/disasters/$ID/resources?radius_km=5"
```

```
 0.449 km  rescue    Central Park Staging Area
 3.313 km  shelter   Midtown Relief Shelter
 3.790 km  food      Harlem Community Kitchen
 4.893 km  hospital  Bellevue Hospital Center
```

Watch the cache work. The first call is a MISS, the second is a HIT:

```bash
curl -sD- -o /dev/null "localhost:8000/api/v1/disasters/$ID/reports" | grep -i x-cache
curl -sD- -o /dev/null "localhost:8000/api/v1/disasters/$ID/reports" | grep -i x-cache
```

Kill the external providers and see what happens:

```bash
docker compose stop mock-services
curl -s "localhost:8000/api/v1/disasters/$ID/reports" | python -m json.tool | head -20
```

You still get a 200, with the last known reports and honest metadata:

```json
"meta": {
  "cache": "STALE",
  "degraded": true,
  "sources": { "mock-social-api": "failed", "mock-official-api": "failed" }
}
```

Disaster CRUD keeps working throughout. Bring the providers back with
`docker compose start mock-services`.

Live updates:

```bash
curl -N localhost:8000/api/v1/events/stream
```

Create or update a disaster in another terminal and the event shows up straight
away.

---

## Architecture

A request flows through these layers in order:

1. Middleware assigns a request id, applies CORS and rate limiting, and adds
   security headers.
2. `api/v1/routes` validates the request with Pydantic and checks the caller's
   role.
3. `services` applies the business rules, including ownership checks, and owns
   the transaction boundary.
4. `repositories` runs the SQL and the PostGIS calls against PostgreSQL.
5. `cache` sits in front of anything external, backed by Redis with a small in
   process tier in front of it.
6. `realtime` publishes events to Redis pub/sub, which are fanned out to
   WebSocket and SSE clients.
7. `integrations` makes the outbound HTTP calls, wrapped in timeouts, retries, a
   circuit breaker and a rate limiter.

The external dependencies are PostgreSQL with PostGIS, Redis, the mock provider
service on port 9000, Nominatim for geocoding, and optionally the Anthropic API.
An arq worker runs alongside the API to pre-warm caches, and the API works fine
without it.

The layering rule is simple and I stuck to it:

- Routes parse and validate HTTP, check roles, and shape responses. They never
  touch the database and hold no business rules.
- Services hold the business rules, ownership checks, orchestration and
  transaction boundaries. They never build SQL and know nothing about HTTP.
- Repositories own the SQL and the PostGIS calls. They know nothing about HTTP
  or permissions.
- Integrations own outbound calls, normalisation and resilience. They never touch
  the database.

The benefit is concrete. Business rules can be tested without starting a web
server, and the geospatial SQL can be read without wading through permission
logic.

### Project layout

```
app/
  main.py            app factory, lifespan, middleware
  core/              config, logging, errors, security, deps, middleware
  api/v1/routes/     auth, disasters, resources, reports, events, health
  schemas/           Pydantic request and response models
  db/                engine, session and SQLAlchemy models
  repositories/      data access, geo.py holds the PostGIS helpers
  services/          business logic, classification.py is the priority scorer
  integrations/      http, resilience, nominatim, llm, gazetteer, providers
  cache/             backend abstraction and the two tier cache
  realtime/          event bus over Redis pub/sub
  workers/           arq background jobs, optional

mock_services/       stand in social and official providers
migrations/          Alembic, 0001 creates extensions, tables and indexes
scripts/             seed, export_openapi, build_postman, demo_events
tests/               108 tests, see tests/README.md
web/index.html       demo map client
docs/                architecture.md, openapi.json, postman_collection.json
```

---

## Data model

```sql
users      (id, username UNIQUE, password_hash, role, is_active, timestamps)

disasters  (id, title, description,
            location_name       TEXT,
            location            GEOGRAPHY(POINT,4326),   -- nullable
            location_source     TEXT,
            location_confidence NUMERIC(3,2),
            tags                TEXT[] NOT NULL DEFAULT '{}',
            status              disaster_status,
            created_by          UUID REFERENCES users ON DELETE RESTRICT,
            timestamps)

resources  (id, name, type resource_type,
            location GEOGRAPHY(POINT,4326) NOT NULL,     -- required here
            address, capacity, contact, is_active,
            disaster_id UUID NULL REFERENCES disasters ON DELETE SET NULL,
            timestamps)

audit_log  (id, entity_type, entity_id, action, actor_id, actor_username,
            changes JSONB, request_id, created_at)
```

Indexes and why they exist:

- `ix_resources_location` is a GiST index, which is what turns `ST_DWithin` into
  an index scan
- `ix_disasters_location` is the same thing, for proximity filtered disaster lists
- `ix_disasters_tags` is a GIN index, so `tags @> ARRAY['flood']` is a lookup
  rather than a scan over every row's array
- `ix_disasters_title_trgm` is GIN with pg_trgm, because `?q=` uses a leading
  wildcard which a btree cannot serve
- `ix_disasters_status_created_at` is a composite btree on status and
  `created_at DESC`, because the common list query filters on status and then
  sorts by recency, and Postgres can satisfy both from one index with no separate
  sort step

Decisions worth calling out:

- Incident location is nullable, resource location is not. An incident's location
  is inferred from free text and that can legitimately fail. Refusing to record a
  report because a third party geocoder was down would be the wrong behaviour for
  a disaster tool. A resource with no coordinates, on the other hand, can never
  be returned by a proximity search, so it would be dead data.
- Tags are a `TEXT[]` with a GIN index rather than a join table. They are a
  small, closed, read mostly vocabulary that is only ever queried by containment.
  A join table would add two joins to every list query for referential integrity
  nothing here needs. If tags ever gained attributes of their own that
  calculation flips.
- Location provenance is stored, not just coordinates. `location_source` and
  `location_confidence` travel with every point, because someone dispatching a
  rescue team needs to know the difference between a rule matching a place
  dictionary and a human typing it in.
- Primary keys are UUIDs. Ids appear in URLs and go out over WebSocket, and a
  sequential integer would leak how many incidents exist and invite enumeration.
- `audit_log` has no foreign key to users. A trail has to outlive the rows it
  describes. RESTRICT would block user deletion forever and CASCADE would destroy
  exactly the evidence you kept it for.

---

## API

Full interactive docs are at `/docs`. The generated spec is committed at
`docs/openapi.json` and there is a Postman collection at
`docs/postman_collection.json`.

Authentication:

- `POST /api/v1/auth/login` returns a JWT
- `GET /api/v1/auth/me` describes the current token holder

Disasters:

- `POST /api/v1/disasters` for admin or contributor, resolves the location from
  the description
- `GET /api/v1/disasters` supports `tag`, `status`, `q`, `created_by`, `lat`,
  `lng`, `radius_km`, `sort`, `limit` and `offset`
- `GET /api/v1/disasters/{id}`
- `PATCH /api/v1/disasters/{id}` for an admin, or the person who created it
- `DELETE /api/v1/disasters/{id}` for an admin only, returns 204
- `GET /api/v1/disasters/{id}/audit` for the change history

Resources and reports:

- `GET /api/v1/disasters/{id}/resources` takes `lat`, `lng`, `radius_km`, `type`
  and `limit`
- `GET /api/v1/disasters/{id}/reports` takes `priority`, `limit` and `refresh`
- `GET /api/v1/resources` and `POST /api/v1/resources`, the latter admin only

Real time and health:

- `GET /api/v1/events/stream` for Server Sent Events
- `WS /api/v1/events/ws` for WebSocket
- `GET /api/v1/health` and `GET /api/v1/health/ready`

List endpoints return an envelope rather than a bare array, because a bare array
leaves nowhere to put a total or a degraded flag without a breaking change:

```json
{ "items": [], "meta": { "total": 42, "limit": 20, "offset": 0, "has_more": true } }
```

Every error is an RFC 9457 problem document served as `application/problem+json`:

```json
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

One shape across the whole API means a client needs one parser instead of one per
endpoint, and `request_id` ties any reported failure straight back to a log line.
It is also returned as an `X-Request-ID` header on every response, along with
`X-Cache`, `X-Cache-Age`, `X-Degraded`, `X-RateLimit-Remaining` and
`X-Response-Time-ms` where relevant.

---

## Technical decisions

### Why PostgreSQL and PostGIS

Geospatial search was the deciding requirement. PostGIS gives a real spatial
index and spheroid accurate distances in metres. The alternatives each cost
something that mattered here.

- SQLite with a Haversine calculation in SQL has no spatial index, so every query
  is a full scan, and the maths breaks near the poles and the antimeridian.
- MongoDB has good geo support, but this data is very relational, with incidents
  linked to users and resources plus an audit trail, and I wanted real foreign
  keys and transactions.
- Postgres without PostGIS would work using a bounding box prefilter plus
  Haversine, but that is reimplementing, worse, what PostGIS already does.

Postgres also covered everything else in one system: `TEXT[]` with GIN for tags,
pg_trgm for the search box, JSONB for audit diffs, enums for closed vocabularies,
and transactions so an incident and its audit row commit together.

### How the geospatial search works

`GET /disasters/{id}/resources` compiles to roughly this:

```sql
SELECT r.*, ST_Distance(r.location, $point) / 1000.0 AS distance_km
FROM resources r
WHERE r.is_active
  AND ST_DWithin(r.location, $point, $radius_km * 1000)
ORDER BY ST_Distance(r.location, $point)
LIMIT $limit;
```

where `$point` is `ST_SetSRID(ST_MakePoint($lng, $lat), 4326)::geography`.

Four things are doing the work.

First, the column is `geography`, not `geometry`. With `geometry` at SRID 4326,
`ST_Distance` returns degrees, and a degree is not a distance. One degree of
longitude is about 111 km at the equator and 0 km at the pole. Getting real
metres out of `geometry` means projecting into a local coordinate system such as
UTM, which means picking the right zone for every query and handling data that
spans zones. `geography` does the spheroid maths and returns metres.

Second, it uses `ST_DWithin` and not `ST_Distance(...) < r`. These return
identical results and have completely different query plans. `ST_DWithin` is
rewritten by the planner into a bounding box overlap that the GiST index can
answer, and exact distances are then computed only on the rows that survive.
`ST_Distance(...) < r` has to be evaluated for every row before the comparison
can happen, which forces a sequential scan.

I measured this on the real schema with 200,000 resources using
`EXPLAIN (ANALYZE, BUFFERS)`. The full plans are in `docs/architecture.md`.

- `ST_DWithin` gives a Bitmap Index Scan on `ix_resources_location`, 257 buffers,
  6.47 ms
- `ST_Distance(...) < r` gives a Parallel Seq Scan, 3,948 buffers, 311.92 ms

That is roughly 48 times faster and 15 times fewer buffer reads for the same
answer.

Third, distance is computed and sorted in SQL. That way `ORDER BY distance LIMIT
20` really does return the twenty nearest rows, instead of fetching every match
and sorting in Python.

Fourth, longitude comes first. `ST_MakePoint` takes x and y, and x is longitude.
Passing them the other way round gives a point that is perfectly valid and
completely wrong. The swap happens in exactly one place,
`app/repositories/geo.py::geography_point`, which takes named `lat` and `lng`
arguments so no caller can get it wrong.

There is a regression test for the antimeridian in
`tests/test_geospatial.py::test_antimeridian_is_handled_correctly`. Two points
either side of 180 degrees must come out about 112 km apart, not about 19,000 km.
That is the case that breaks flat plane distance maths.

`radius_km` is capped at `MAX_RADIUS_KM`, which defaults to 500. An unbounded
radius would let a single request scan the whole table and defeat the index.

### How location resolution works

Step 1 extracts a place name from the description. If
`ANTHROPIC_API_KEY` is set an LLM does it, otherwise the rule based extractor
does. Either way the output is a string like "Manhattan, NYC" with a confidence
score, and the result is cached for 24 hours.

Step 2 resolves that name to coordinates. It checks the cache first, then
Nominatim, then falls back to the bundled gazetteer. The result is cached for 7
days.

So "Heavy flooding has affected Manhattan, NYC." becomes (40.7831, -73.9712) with
a source of `rules:cue_known+gazetteer` and a confidence of 0.63.

Extraction and geocoding are deliberately separate steps. It is tempting to ask
an LLM for coordinates directly, but a model will happily return a confident,
plausible, wrong latitude and longitude and nothing downstream can detect it. So
the model is only ever asked for a name, and a real geocoder turns names into
points.

The rule based extractor looks for locative cues such as `in`, `near`, `at`,
`affected` and `struck`, followed by capitalised runs, and scores them against a
bundled gazetteer of about 150 places.

- 0.90 for `rules:cue_known`, a known place after a locative cue
- 0.75 for `rules:known`, a known place anywhere in the text
- 0.55 for `rules:cue`, an unknown proper noun after a cue, still sent to the
  geocoder
- 0.35 for `rules:proper_noun`, a capitalised run and nothing more

It is rules rather than a model because the text here is short and formulaic, and
because the result is deterministic, instant, free and explains itself. The LLM
path handles messier input when a key exists.

Confidence is multiplicative, so a 0.90 extraction feeding a 0.70 geocoder gives
0.63. Uncertainty compounds and the number should say so.

Resolution failure is never fatal. If nothing resolves, the incident is still
created with `location.point` as null and the source recorded as `unresolved`.

One note on Nominatim. It needs no API key, but it rate limits to one request per
second and blocks clients it considers abusive, so from inside Docker it often
returns `403 Access denied`. This is handled rather than broken. You will see
`geocoder_unavailable_using_gazetteer` in the logs and everything still resolves
through the offline gazetteer. Set `GEOCODER_ENABLED=false` to skip the attempt
entirely.

### How caching works

A read goes through these steps:

1. Check the in process L1 dict. A hit returns immediately with `X-Cache: HIT`.
2. Check Redis. If the entry is younger than its TTL it is returned, also as a
   hit.
3. Otherwise the entry is missing or soft expired, and the caller tries to take
   an atomic `SET NX EX` lock.
4. The caller that wins the lock calls the origin. On success the value is stored
   and returned as `X-Cache: MISS`. On failure, stale data is served as
   `X-Cache: STALE` with `degraded: true`, or a 502 is returned if nothing is
   cached at all.
5. Callers that lose the lock serve stale data if there is any, and otherwise
   wait up to two seconds for the winner before fetching themselves.

Three ideas are doing the real work.

Soft and hard expiry. The stored envelope records when it was fetched. The TTL is
a soft expiry, so past it the value is stale but still usable, while the Redis
key itself lives for the TTL plus a stale grace period. When an origin is down we
answer from stale data flagged as degraded instead of returning a 502. For
disaster reports, five minute old data is far more useful than an error page.

Single flight. On a cold key, many concurrent requests would otherwise all call
the origin at once, which is a cache stampede arriving exactly when the origin is
least able to cope. One caller wins an atomic `SET NX` lock and fetches, and the
rest serve stale or wait briefly. There is a test that fires 20 concurrent
callers at a cold key and asserts a single origin call.

The L1 tier is not really about Redis latency. It collapses the burst of
identical calls a single page render produces into one Redis round trip. It is
per worker and short lived, so any cross worker inconsistency is bounded by
`CACHE_L1_TTL`.

TTLs were chosen by how fast the underlying data actually changes.

- Geocoding results last 7 days, because a city does not move, and this is also
  what keeps us inside Nominatim's one request per second limit.
- Place extraction lasts 24 hours. It is deterministic for a given description,
  so the TTL only bounds prompt changes.
- Reports last 5 minutes, because social chatter has to stay reasonably fresh.
- Official bulletins last 15 minutes, since they change slowly.
- A degraded result lasts 60 seconds, so a recovering provider is picked up
  quickly instead of being masked for the full window.
- Stale grace is 1 hour, which is how long stale data may be served when the
  origin is down.

Invalidation is targeted, never a flush. Editing a disaster's description or
location drops that disaster's report cache, because the provider query is
derived from those fields, but it leaves the geocoding cache alone since those
entries cost a rate limited external call to rebuild.

### How external API failures are handled

Three different failures need three different tools, and conflating them is the
usual mistake.

- Retry handles a transient blip, one dropped packet or one 503 during a deploy.
  It is useless against a sustained outage.
- A circuit breaker handles a sustained outage. Retrying a dead service burns our
  own latency budget and keeps hammering something that is trying to recover.
- A rate limiter handles a published quota. Nothing is broken, we simply must not
  exceed it.

Retries use exponential backoff with full jitter. Without jitter, every client
that failed at the same moment retries at the same moment and reconverges into a
synchronised herd.

Only 5xx responses, timeouts and 429 are retried. A 400 or 422 will fail
identically every time, so retrying only multiplies the latency of a request that
was always going to fail. `http.py` classifies every response as either transient
or permanent, and only transient errors are retried or counted against the
breaker.

The breaker runs per provider, per process.

The breaker starts closed and calls pass through. After five
consecutive failures it opens and rejects calls immediately without attempting
them. Thirty seconds later it moves to half open and allows exactly one trial
call through. If that call succeeds it closes again, and if it fails it reopens
and the timer restarts.

Half open admits exactly one probe, because letting full load through the instant
the timer expires would flatten a service that is still fragile. The counter
resets on any success, so isolated blips never accumulate towards tripping it.

The fallback chain in order is fresh cache, then stale cache, then a bundled
fixture or the gazetteer, then an empty result marked as degraded. Only a cold
cache combined with a dead upstream produces a 502.

Partial failure is a first class outcome. The two providers are fetched
concurrently with `asyncio.gather(..., return_exceptions=True)`. That argument is
essential, because without it the first failure cancels the sibling task and a
working provider's data gets thrown away. One provider being down still returns
the other's reports with `degraded: true` and a per provider `sources` map
showing `ok`, `empty`, `failed` or `circuit_open`.

When stale data is served, `sources` reports why each provider failed on this
request, read from live breaker state, rather than the provenance stored with the
cached payload which would usually say `ok` and be misleading.

The blast radius is contained. These calls happen only behind the reports
endpoint, so a dead social API degrades that one feed and cannot affect disaster
CRUD. There is a test for exactly that.

The mock providers run as a separate container on their own port rather than as
an in process stub, so timeouts, retries and the breaker are exercised against a
real socket that can really be slow, really return 503, and really be stopped mid
demo. They expose a chaos endpoint for this:

```bash
curl -X POST "localhost:9000/_control/chaos?failure_rate=0.8&latency_ms=3000"
curl -X POST "localhost:9000/_control/chaos?failure_rate=0.0"
```

### Real time updates

Events are published to a Redis pub/sub channel and fanned out to both a
WebSocket and an SSE endpoint. Two transports because they suit different
consumers. WebSocket is bidirectional and drives the browser map. SSE is plain
HTTP, needs no client library, survives proxies that mishandle upgrades, and can
be checked with a single `curl -N`.

The events are `disaster_created`, `disaster_updated` which carries
`changed_fields`, `disaster_deleted` and `resource_created`.

Using pub/sub rather than a process local list of connections matters as soon as
there is more than one uvicorn worker. A connection lives in exactly one process,
so an in process broadcast would reach only the clients that happened to land on
the worker handling the write, and everyone else would silently miss the event.

Events are published after the commit, never before. Announcing a change that can
still roll back would let a client fetch the resource and get a 404.

Delivery is best effort and at most once. There is no replay buffer and no
acknowledgement, so a client that disconnects misses what happened and has to
re-fetch on reconnect. That is the right trade for live situational awareness,
where the REST API remains the source of truth and this only says that something
changed.

### Authentication and authorization

JWT with HS256, and Argon2id password hashes. HS256 because a single service both
issues and verifies tokens, so a shared secret is enough. The moment a second,
independently deployed verifier existed, RS256 or EdDSA would be correct instead.
Argon2id because it is memory hard, so a GPU attacker gains far less than with
bcrypt.

Authorization is split across two layers deliberately.

- Role gates live in route dependencies. "Only an admin may delete" is a property
  of the endpoint, it can be decided before any data is loaded, and it shows up
  in the OpenAPI document.
- Ownership checks live in the service layer, because "a contributor may edit
  only their own incident" cannot be decided until the record has been loaded.

The rule this demonstrates:

- an admin can patch anything and delete anything
- a contributor can patch their own record, gets a 403 on someone else's, and
  gets a 403 on delete either way
- a responder is read only

Both contributors hold the same role and both pass the route's role gate, so only
a record level check can tell them apart. That is the whole point of the split,
and it is covered by
`tests/test_authz_rules.py::test_contributor_cannot_update_someone_elses_disaster`,
which also asserts that the record was genuinely left unchanged.

Other things worth mentioning:

- the request pipeline re-reads the user from the database rather than trusting
  the role claim in the token, so deactivating a user takes effect immediately
- login answers identically for an unknown username and a wrong password, and
  verifies against a dummy hash when the user is missing, so response timing does
  not leak account existence either
- `/auth/login` has a tighter rate limit than the rest of the API
- `algorithms` is pinned to a single value when decoding, which is how the
  classic `alg: none` and HS/RS confusion attacks are prevented
- the unhandled exception handler never echoes exception text back to the caller

---

## Testing

108 tests, all offline and deterministic. Every external HTTP call is intercepted
by `httpx.MockTransport`.

- `tests/test_disasters_api.py` covers create, read, list, patch, filtering,
  pagination and the audit trail
- `tests/test_validation_errors.py` covers the error contract, the 422, 404 and
  401 shapes, blank input, unknown fields and bad enums
- `tests/test_authz_rules.py` covers ownership, role gates, deactivation, and
  expired and forged tokens
- `tests/test_geospatial.py` covers radius boundaries, distance accuracy,
  ordering and the antimeridian
- `tests/test_cache_and_resilience.py` covers hit, miss and stale paths, single
  flight, retry, the breaker and the rate limiter
- `tests/test_reports_integration.py` covers normalisation, caching, and partial
  and total failure
- `tests/test_classification.py` covers the priority scorer and place extraction

Mapping to what the brief asked for:

- one API endpoint is `test_create_disaster_resolves_location_from_description`
  and `test_list_disasters_filters_by_tag`
- one validation or error scenario is
  `test_missing_required_fields_returns_422_problem_document` and
  `test_unknown_disaster_returns_404_not_500`
- one important business rule is
  `test_contributor_cannot_update_someone_elses_disaster` and
  `test_only_admin_may_delete`
- one external integration or mock is
  `test_provider_payloads_are_normalised_into_one_shape` and
  `test_second_request_is_a_cache_hit_and_does_not_call_the_provider`

Two things about the approach are worth highlighting. The cache tests count
upstream calls rather than trusting the `X-Cache` header, because the header
could simply be wrong, so counting invocations of the mock transport is what
actually proves the network was avoided. And the geospatial tests run against
real PostGIS, because the claim under test is that `ST_DWithin` returns the right
rows with the right distances, and mocking the database would just test the mock.

Isolation comes from running each test inside a transaction that is rolled back
afterwards, using `join_transaction_mode="create_savepoint"` so the application's
own commit calls are genuinely exercised.

---

## Assumptions

- Mock users are enough, as the brief says. There is no registration, password
  reset or refresh token flow.
- Reports are not persisted. They are external, ephemeral and cached, so they are
  normalised and cached rather than stored. Persisting them would add a sync and
  dedup problem the brief does not ask for.
- Resource data is reference data maintained by admins rather than crowd sourced,
  so resource writes are admin only while incident writes are not.
- One incident has one location. Real events such as a cyclone track or a river
  flood are lines or polygons, and PostGIS handles those natively, but the brief
  asks for latitude and longitude.
- Approximate city level coordinates are acceptable. The gazetteer stores
  centroids, which is fine for radius search at city scale, and the confidence
  score communicates it.
- Reads are public so the map demo renders without credentials. Only writes are
  authenticated. A real deployment would probably authenticate reads too.
- Single region and moderate scale, so offset pagination and per process circuit
  breakers are fine.

---

## Trade-offs

Things I simplified on purpose, and what I would do instead with more time.

- Offset pagination is simple and supports arbitrary sorts and random page
  access, but it degrades on deep pages because the database still walks the
  skipped rows. Keyset pagination is the answer.
- Delete is a hard delete, which is simple and predictable, with history
  surviving in the audit log. Soft delete would be better, but it infects every
  query with `WHERE deleted_at IS NULL`, so half doing it is worse than not doing
  it.
- The classifier is rule based, so it is deterministic, explainable, instant and
  free, but it cannot read negation, sarcasm, or languages other than English. I
  would keep the rules as a pre-filter and put a trained classifier behind them.
- Circuit breakers are per process, which needs no coordination, but N workers
  each spend N failures learning the same thing. Shared state in Redis would fix
  it.
- Rate limiting uses a fixed window, which is one atomic `INCR` per request but
  tolerates up to twice the limit across a window boundary. A sliding window or
  token bucket would be more accurate.
- `?q=` uses ILIKE with a trigram index, which is good enough for an operator
  search box. Real relevance ranking wants `tsvector` and `ts_rank`.
- JWTs have no revocation. Stateless and simple, but a stolen token stays valid
  until it expires. Short access tokens plus refresh tokens plus a denylist would
  fix it, and `jti` is already in the claims for exactly that.
- The in memory cache fallback means the app still boots without Redis, but the
  cache stops being shared and events stop crossing workers. In production I
  would set `CACHE_ALLOW_MEMORY_FALLBACK=false` and let the health check pull the
  instance out of rotation.
- There is no CI pipeline. Out of scope for the time budget. GitHub Actions
  running ruff and pytest against a PostGIS service container would be next.

---

## Known limitations and future work

- The reports feed is not paginated. It is capped at `limit=200` from a bounded
  number of providers so it cannot grow unboundedly, but it is not paged.
- The worker is at least once with no deduplication. That is harmless here
  because the only job is an idempotent cache refresh, but any job with side
  effects would need an idempotency key.
- WebSocket connections are not authenticated. That is acceptable while events
  carry only summary data already readable through the public list endpoint, but
  a private deployment would need a token on the upgrade request.
- There is no observability beyond structured logs. Prometheus metrics for cache
  hit ratio, breaker state and upstream latency, plus OpenTelemetry tracing,
  would be the obvious next step. `request_id` already threads through
  everything, so traces would slot in cleanly.
- The gazetteer covers about 150 places. It is a fallback, not a geocoder. With a
  key configured, or with Nominatim reachable, coverage is global.
- The demo map does not cluster markers. That is fine at 33 resources and would
  not be at thousands.
- No load testing has been done. The EXPLAIN ANALYZE numbers above are single
  query measurements, not a concurrency benchmark.

---

## Configuration

Every tunable lives in `.env.example` with a comment, and is read once at startup
into a frozen settings object in `app/core/config.py`. Nothing else in the
codebase reads `os.environ`, so there is one answer to where a value comes from.

The committed defaults are local development values and contain no real secrets.
`.env` itself is gitignored. Before any real deployment, set `JWT_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The application logs an error on startup if it is running in production with the
default secret still in place.

---

## AI-assisted development

Tool used: Claude (Anthropic), through Claude Code, as a pair programming
assistant.

What it helped with:

- Scaffolding boilerplate, including the Alembic migration skeleton, Pydantic
  model definitions, the Docker and Compose setup, and the repetitive parts of
  the test suite
- Drafting the bundled gazetteer data and the mock providers' sample content
- First drafts of docstrings and this README, from decisions I had already made
- Acting as a reviewer to talk through where the ownership check belonged, and
  whether the circuit breaker should wrap the retry loop or sit inside it

What I designed and decided:

- The overall architecture and the layering rule, including the deliberate split
  of authorization across the route layer for roles and the service layer for
  ownership
- Every database decision: geography over geometry, nullable incident location
  against non-null resource location, `TEXT[]` with GIN over a tag join table,
  storing resolution provenance, and the choice of each index
- The caching strategy, meaning two tiers, soft and hard expiry for stale while
  revalidate, single flight locking, per namespace TTLs and targeted invalidation
- The resilience design, meaning which errors are transient against permanent,
  retrying only the transient ones, the breaker's single probe half open
  behaviour, and ordering the breaker around the retry so a run of retries counts
  as one failure
- Keeping the LLM away from coordinates and restricting it to name extraction

What I reviewed, corrected and validated. I read and ran everything. These are
the bugs I found and fixed during development:

1. The place extractor ran past sentence boundaries, so "a warehouse in Brooklyn.
   Smoke is drifting" produced the candidate "Brooklyn. Smoke", which then failed
   to geocode. Fixed with an abbreviation aware split so "St. Louis" still
   survives, with regression tests for both halves.
2. The priority classifier double counted overlapping signals. "baby formula"
   matched the urgent need lexicon and the vulnerable group regex on the same
   word, which inflated ordinary supply requests into the critical bucket. Fixed
   by tracking matched character spans.
3. `os.environ.setdefault` in the test harness was a silent no-op under Docker,
   because Compose already injects `APP_ENV`, so the suite ran against the
   development database and dropped its tables. Changed to explicit assignment.
4. Rate limit rejections raised from middleware escaped the registered exception
   handlers, which live inside the routing layer, and surfaced as bare 500s. The
   problem document is now constructed directly in the middleware.
5. `meta.sources` on a stale response echoed the cached payload's provenance
   instead of why the live fetch had just failed. It is now read from live
   circuit breaker state.
6. A strict `default-src 'none'` CSP was being applied to the demo page and
   blocking Leaflet. The asset serving paths are now exempted rather than the
   policy being weakened everywhere.
7. `pyproject.toml` was baked into the image but not mounted, so pytest
   configuration changes appeared to do nothing.

I also ran the EXPLAIN ANALYZE benchmark myself to confirm the `ST_DWithin` claim
with real numbers rather than asserting it from memory, and verified the
degradation path by actually stopping the provider container.

---

## License

MIT
