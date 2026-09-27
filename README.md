# Disaster Response Coordination Platform

Backend for coordinating disaster response. Stores incidents, works out where they are from a
free text description, finds resources near them, pulls community reports from external
providers, and pushes live updates to connected clients.

Python 3.12, FastAPI, PostgreSQL 16 with PostGIS, Redis, SQLAlchemy 2.0 async. No API keys needed.

## Setup

Needs Docker and Docker Compose, nothing else.

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed
```

Swagger UI at http://localhost:8000/docs, demo map with live updates at
http://localhost:8000/demo/, health at http://localhost:8000/api/v1/health/ready.

Seeded accounts: `admin/admin123` (full access), `contributor/contributor123` (create, and
update only their own records), `contributor2/contributor123` (second account, shows the
ownership rule), `responder/responder123` (read only).

`docker compose exec api pytest` runs 108 tests against a separate test database, see
`tests/README.md`. The generated spec is at `docs/openapi.json` and there is a Postman
collection at `docs/postman_collection.json`.

## Architecture

Requests flow through four layers and I kept the boundaries strict:

1. `api/v1/routes` validates with Pydantic and checks the caller's role. No database access, no
   business rules.
2. `services` holds business rules, ownership checks and transaction boundaries. Builds no SQL,
   knows nothing about HTTP.
3. `repositories` owns the SQL and the PostGIS calls. Knows nothing about permissions.
4. `integrations` makes outbound calls wrapped in timeouts, retries, a circuit breaker and a
   rate limiter. Never touches the database.

Alongside those, `cache` sits in front of anything external, `realtime` publishes to Redis
pub/sub for WebSocket and SSE clients, and an optional arq worker pre-warms caches. External
dependencies are PostgreSQL with PostGIS, Redis, a mock provider on port 9000, and Nominatim.
The Anthropic API is optional.

Request lifecycle, measured query plans and failure mode details are in `docs/architecture.md`.

## Technical decisions

**Why PostgreSQL and PostGIS.** Geospatial search decided it: PostGIS is the only option here
with a real spatial index and spheroid accurate distances in metres. SQLite has no spatial
index so every query is a full scan. MongoDB has decent geo support but this data is very
relational and I wanted foreign keys and transactions. Postgres also covered tags (`TEXT[]`
with GIN), the search box (pg_trgm), audit diffs (JSONB) and enums in one system.

**Geospatial search.** `ST_DWithin(location, point, radius_km * 1000)` against a
`geography(Point, 4326)` column with a GiST index, ordered by `ST_Distance`.

Four things matter here. The column is `geography` not `geometry`, because with `geometry` at
SRID 4326 distances come back in degrees and a degree is not a distance. It uses `ST_DWithin`
rather than `ST_Distance(...) < r`, because the planner rewrites `ST_DWithin` into a bounding
box overlap the index can answer while `ST_Distance` has to run on every row first. Distance is
computed and sorted in SQL so `LIMIT` returns the genuinely nearest rows. And longitude comes
first in `ST_MakePoint`, so that swap happens in exactly one helper.

Measured on 200,000 rows with `EXPLAIN (ANALYZE, BUFFERS)`: `ST_DWithin` gives a Bitmap Index
Scan, 257 buffers, 6.47 ms. `ST_Distance(...) < r` gives a Parallel Seq Scan, 3,948 buffers,
311.92 ms. Roughly 48x faster for the same answer. `radius_km` is capped so one request cannot
scan the whole table.

**Location resolution.** Two steps: extract a place name from the description, then geocode it.
Extraction uses an LLM if `ANTHROPIC_API_KEY` is set, otherwise a rule based extractor matching
locative cues against a bundled gazetteer of about 150 places. Geocoding goes cache, then
Nominatim, then the offline gazetteer.

The LLM is only ever asked for a name, never coordinates, because a model will return a
confident, plausible, wrong latitude and longitude that nothing downstream can detect.
Confidence is multiplicative (0.90 extraction times 0.70 geocoder gives 0.63) and both source
and confidence are stored on the record. Failure is never fatal, the incident is still created
with a null location. Nominatim often returns 403 from inside Docker, which is handled: you
will see `geocoder_unavailable_using_gazetteer` in the logs and everything still resolves.

**Caching.** Two tiers, a short in process dict in front of Redis. The stored envelope records
when it was fetched, so the TTL is a soft expiry while the Redis key lives for the TTL plus a
stale grace period. When an origin is down we serve that stale data flagged as degraded instead
of a 502, because five minute old reports are more useful to a responder than an error page. On
a cold key one caller wins an atomic `SET NX` lock while the rest serve stale or wait briefly,
which stops a cache stampede.

TTLs follow how fast the data changes: 7 days for geocoding (a city does not move, and it keeps
us inside Nominatim's rate limit), 24 hours for extraction, 5 minutes for reports, 15 minutes
for official bulletins, 60 seconds for a degraded result. Invalidation is targeted, never a flush.

**External API failures.** Three different failures need three different tools. Retry handles a
transient blip, a circuit breaker handles a sustained outage because retrying a dead service
burns our own latency budget, and a rate limiter handles a published quota.

Retries use exponential backoff with full jitter and only fire on 5xx, timeouts and 429, since
a 400 will fail identically every time. The breaker opens after five consecutive failures,
waits 30 seconds, then allows exactly one trial call. It sits outside the retry loop, so three
retries count as one failure rather than three. Providers are fetched concurrently with
`asyncio.gather(..., return_exceptions=True)`, and that argument matters because without it one
failure cancels the sibling task and a working provider's data is thrown away. One provider
down still returns the other's reports with `degraded: true`. Only a cold cache plus a dead
upstream gives a 502. The mock providers run as a separate container behind a real HTTP
boundary, so timeouts and the breaker are exercised against a socket that can really be stopped.

**Authorization.** Split across two layers on purpose. Role gates live in route dependencies,
because "only an admin may delete" can be decided before loading data. Ownership checks live in
the service layer, because "a contributor may edit only their own incident" cannot be decided
until the record is loaded. Two contributors share a role and both pass the route gate, so only
a record level check tells them apart.

## Assumptions

- Mock users are enough. No registration, password reset or refresh tokens.
- Reports are external and ephemeral, so they are normalised and cached rather than persisted.
- Resources are reference data maintained by admins, so resource writes are admin only.
- One incident has one point location. Real events are often lines or polygons.
- Reads are public so the demo map renders without credentials. Only writes are authenticated.

## Trade-offs

- Offset pagination is simple but degrades on deep pages. Keyset would fix it.
- Delete is hard, with history in the audit log. Soft delete infects every query with a filter.
- The priority classifier is rule based, so it is deterministic and explainable but cannot read
  negation or other languages.
- Circuit breakers are per process, so each worker spends failures learning the same thing.
- Rate limiting uses a fixed window, which tolerates up to twice the limit across a boundary.
- `?q=` uses ILIKE with a trigram index rather than proper full text ranking.
- JWTs have no revocation. `jti` is in the claims ready for a denylist.
- Without Redis the app still boots on an in process cache, but it stops being shared.

## Known limitations

- The reports feed is capped rather than paginated.
- The worker is at least once with no deduplication, which is harmless because the only job is
  an idempotent cache refresh.
- WebSocket connections are not authenticated. Events carry only summary data that is already
  public, but a private deployment would need a token.
- No metrics or tracing yet, only structured logs. `request_id` threads through everything.
- The gazetteer covers about 150 places. It is a fallback, not a geocoder.
- No load testing. The numbers above are single query measurements.

## Configuration

Everything is in `.env.example`, read once at startup into a settings object in
`app/core/config.py`. Nothing else reads `os.environ`. The committed defaults are local
development values, `.env` is gitignored, and the app logs an error if it starts in production
with the default `JWT_SECRET`.

## AI-assisted development

Tool used: Claude, through Claude Code, as a pair programming assistant.

Helped with: scaffolding boilerplate such as the Alembic migration skeleton, Pydantic models
and the Docker setup; repetitive parts of the test suite; the gazetteer data and mock provider
content; and acting as a reviewer to talk through where the ownership check belonged.

What I decided: the architecture and layering rule, splitting authorization across the route
and service layers, every database decision (geography over geometry, nullable incident
location, `TEXT[]` with GIN over a join table, storing resolution provenance, each index), the
caching strategy, the resilience design including ordering the breaker around the retry, and
keeping the LLM away from coordinates.

What I reviewed and fixed. I read and ran everything. Bugs I found and corrected:

1. The place extractor ran past sentence boundaries, so "a warehouse in Brooklyn. Smoke is
   drifting" produced "Brooklyn. Smoke". Fixed with an abbreviation aware split so "St. Louis"
   still survives.
2. The classifier double counted overlapping signals, so "baby formula" matched both the urgent
   need lexicon and the vulnerable group regex on the same word and pushed ordinary supply
   requests into the critical bucket. Fixed by tracking matched character spans.
3. `os.environ.setdefault` in the test harness was a silent no-op under Docker, so the suite ran
   against the development database and dropped its tables.
4. Rate limit rejections raised from middleware escaped the exception handlers, which live
   inside the routing layer, and surfaced as bare 500s.
5. `meta.sources` on a stale response reported the cached payload's provenance instead of why
   the live fetch had just failed.
6. A strict CSP was blocking Leaflet on the demo page, and `pyproject.toml` was baked into the
   image but not mounted so pytest config changes appeared to do nothing.

I ran the EXPLAIN ANALYZE benchmark myself rather than asserting the performance claim from
memory, and verified the degradation path by actually stopping the provider container.
