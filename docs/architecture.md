# Architecture notes

Supplementary detail to the README: request lifecycle, the measured query plans
behind the geospatial claims, and the state machines that govern failure handling.

---

## 1. Request lifecycle

What happens on `POST /api/v1/disasters`, end to end.

```
HTTP request
  │
  ├─ RequestContextMiddleware   assign/propagate X-Request-ID, start timer,
  │                             bind request_id into the logging ContextVar
  ├─ CORSMiddleware             origin policy
  ├─ RateLimitMiddleware        Redis INCR on {bucket}:{ip}:{window}
  │                             over limit → 429 problem document, short-circuit
  ├─ SecurityHeadersMiddleware  nosniff, DENY, Referrer-Policy, CSP
  │
  ▼
FastAPI routing → app/api/v1/routes/disasters.py::create_disaster
  │
  ├─ Pydantic validates DisasterCreate   invalid → 422 problem document
  ├─ Depends(require_roles(ADMIN, CONTRIBUTOR))
  │     └─ decode JWT → re-read user from DB → check is_active → check role
  ├─ Depends(get_session)                one AsyncSession for this request
  │
  ▼
app/services/disaster_service.py::create_disaster
  │
  ├─ coordinates supplied?  yes → source="manual", confidence=1.0
  │                         no  → geocoding_service.resolve_from_description()
  │                                 ├─ cache: nlp:place:{hash(description)}
  │                                 │    └─ LLM if key set, else rule extractor
  │                                 └─ cache: geocode:{place}
  │                                      └─ Nominatim → gazetteer fallback
  ├─ disaster_repo.create()      INSERT ... ST_SetSRID(ST_MakePoint(lng,lat),4326)::geography
  ├─ audit_repo.record()         same transaction as the INSERT
  ├─ session.commit()            ← the write becomes durable HERE
  └─ events.publish()            ← only now; publishing earlier could announce
                                   a change that still rolls back
  ▼
DisasterResponse  (201)  +  X-Request-ID, X-Response-Time-ms
  │
  └─ Redis pub/sub "drp:events" → every worker → WebSocket + SSE clients
```

Two orderings in that flow are deliberate and worth stating:

1. **Commit before publish.** A client receiving `disaster_created` and
   immediately issuing a `GET` must not get a 404.
2. **Breaker outside retry** (in the integration clients). A run of three retries
   against a dead provider counts as *one* failure against the breaker, not
   three — otherwise a single request would trip a 5-failure breaker on its own.

---

## 2. Measured query plans

Run against this schema with **200,000 resources**, PostgreSQL 16 + PostGIS 3.4.
Both queries return identical rows.

Reproduce with the SQL in [§2.3](#23-reproducing-the-benchmark).

### 2.1 `ST_DWithin` — the query the application issues

```sql
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT id, name,
       ST_Distance(location, ST_SetSRID(ST_MakePoint(-74.0060,40.7128),4326)::geography)/1000 AS km
FROM resources
WHERE is_active
  AND ST_DWithin(location, ST_SetSRID(ST_MakePoint(-74.0060,40.7128),4326)::geography, 5000)
ORDER BY ST_Distance(location, ST_SetSRID(ST_MakePoint(-74.0060,40.7128),4326)::geography)
LIMIT 50;
```

```
 Limit (actual time=6.376..6.386 rows=50 loops=1)
   Buffers: shared hit=257
   ->  Sort (actual time=6.373..6.376 rows=50 loops=1)
         Sort Method: quicksort  Memory: 37kB
         ->  Bitmap Heap Scan on resources (actual time=5.893..6.332 rows=95 loops=1)
               Filter: (is_active AND st_dwithin(location, '...'::geography, '5000'::double precision, true))
               Rows Removed by Filter: 77
               Heap Blocks: exact=161
               ->  Bitmap Index Scan on ix_resources_location (actual time=0.151..0.151 rows=172 loops=1)
                     Index Cond: (location && _st_expand('...'::geography, '5000'::double precision))
                     Buffers: shared hit=24
 Planning Time: 1.053 ms
 Execution Time: 6.473 ms
```

The line that matters is the `Index Cond`:

```
location && _ST_Expand(point, 5000)
```

The planner rewrote `ST_DWithin` into a **bounding-box overlap** (`&&`) that the
GiST index can answer. It read 172 candidate rows out of 200,000, then applied
the exact spheroid distance to just those — discarding 77 and keeping 95.

### 2.2 `ST_Distance(...) < r` — the naive equivalent

```sql
WHERE is_active
  AND ST_Distance(location, ST_SetSRID(ST_MakePoint(-74.0060,40.7128),4326)::geography) < 5000
```

```
 Limit (actual time=276.982..286.235 rows=50 loops=1)
   Buffers: shared hit=3948
   ->  Gather Merge (actual time=209.399..218.648 rows=50 loops=1)
         Workers Planned: 2
         Workers Launched: 2
         ->  Sort (actual time=190.753..190.755 rows=22 loops=3)
               ->  Parallel Seq Scan on resources (actual time=82.102..190.602 rows=32 loops=3)
                     Filter: (is_active AND (st_distance(location, '...'::geography, true) < '5000'::double precision))
                     Rows Removed by Filter: 66646
                     Buffers: shared hit=3874
 Planning Time: 0.105 ms
 Execution Time: 311.923 ms
```

**Parallel Seq Scan.** `ST_Distance` must be evaluated for every row before the
`<` comparison can be made, so the index is unusable. Postgres threw two extra
worker processes at it and it was still ~48× slower.

### Summary

| | `ST_DWithin` | `ST_Distance < r` |
|---|---|---|
| Plan | Bitmap Index Scan → Bitmap Heap Scan | Parallel Seq Scan (2 workers) |
| Rows examined | 172 | 200,000 |
| Buffers | 257 | 3,948 |
| Execution | **6.47 ms** | **311.92 ms** |

Same results. **~48× faster, 15× fewer buffer reads.** This is the single clearest
argument for the `ST_DWithin` + GiST design.

### 2.3 Reproducing the benchmark

```bash
docker compose exec db psql -U drp -d disaster_response
```

```sql
INSERT INTO resources (name, type, location, is_active)
SELECT 'benchmark_' || g,
       (ARRAY['shelter','hospital','food','water','rescue'])[1 + (g % 5)]::resource_type,
       ST_SetSRID(ST_MakePoint(-74.0060 + (random()-0.5)*4,
                                40.7128 + (random()-0.5)*4), 4326)::geography,
       true
FROM generate_series(1, 200000) g;

ANALYZE resources;
-- ... run both EXPLAIN statements above ...

DELETE FROM resources WHERE name LIKE 'benchmark\_%';
ANALYZE resources;
```

### 2.4 Tag filtering

`?tag=flood` compiles to `tags @> ARRAY['flood']`, served by the GIN index
`ix_disasters_tags`. GIN builds an inverted index — one posting list per distinct
tag pointing at the rows carrying it — so this is a lookup rather than a scan
over every row's array.

Note that `'flood' = ANY(tags)` would *not* use that index. The containment
operator is what makes it work.

---

## 3. Cache state machine

```
                        ┌──────────────────┐
              ┌────────►│  L1 (in-process) │──hit──► return  X-Cache: HIT
              │         └────────┬─────────┘
              │                  │ miss
   request ───┤                  ▼
              │         ┌──────────────────┐
              │         │   L2 (Redis)     │
              │         │ {value,fetched_at│
              │         │  ,ttl}           │
              │         └────────┬─────────┘
              │          age<ttl │  age>=ttl (soft-expired) or absent
              │      ┌───────────┘         │
              │      ▼                     ▼
              │   return            ┌─────────────────┐
              │   X-Cache: HIT      │ SET NX lock     │
              │                     └────┬───────┬────┘
              │                      won │       │ lost
              │                          ▼       ▼
              │                   call origin   stale available?
              │                    │      │      ├─ yes → return  X-Cache: STALE
              │                 ok │      │ fail └─ no  → poll ≤2s for the winner
              │                    ▼      ▼               (then self-serve)
              └──────────── store+return  stale available?
                            X-Cache: MISS  ├─ yes → X-Cache: STALE, degraded:true
                                           └─ no  → 502 upstream_unavailable
```

Key sizes and lifetimes live in `.env.example`; the reasoning for each TTL is in
the README.

The Redis key's own expiry is `ttl + stale_grace`, which is what creates the
window in which stale data remains available after the soft expiry has passed.

---

## 4. Circuit breaker state machine

One breaker per provider (`nominatim`, `anthropic`, `mock-social-api`,
`mock-official-api`), per process.

```
         ┌─────────────────────────────────────────────┐
         │                                             │
         ▼                                             │
    ┌─────────┐   5 consecutive failures      ┌────────┴──┐
    │ CLOSED  │ ────────────────────────────► │   OPEN    │
    │ calls   │                               │ fail fast │
    │ pass    │ ◄──────────────────────────── │ no call   │
    └─────────┘        trial succeeds         └────┬──────┘
         ▲                                         │ 30s elapsed
         │                                         ▼
         │                                  ┌─────────────┐
         └───────── success ──────────────  │  HALF_OPEN  │
                                            │ ONE trial   │
                     trial fails ─────────► │ call only   │
                     (timer restarts)       └─────────────┘
```

Properties that are easy to get wrong and are tested in
`tests/test_cache_and_resilience.py`:

- **Consecutive**, not cumulative: any success resets the counter, so isolated
  blips never accumulate toward tripping.
- **Half-open admits exactly one probe.** Letting full load through the moment the
  timer expires would re-flatten a service that is still fragile.
- **`PermanentError` (4xx) never counts.** A 400 means *our request* was wrong,
  not that the dependency is down; counting it would trip the breaker on our own
  bad input.
- **State is read lazily.** `OPEN → HALF_OPEN` happens on the next read of
  `.state`, so no background timer task is needed.

Live state is exposed at `GET /api/v1/health/ready` under
`checks.external_providers`, which makes "which provider are we currently
skipping, and why?" answerable at a glance.

---

## 5. Concurrency model

Everything is async, on one event loop per worker. The two places that matter:

**Providers are fetched concurrently.**

```python
results = await asyncio.gather(social_task, official_task, return_exceptions=True)
```

Latency is that of the slowest provider, not the sum. `return_exceptions=True` is
not optional here: without it, the first failure cancels the sibling task and a
*working* provider's data is thrown away along with it.

**Blocking work is kept off the loop.** Argon2 hashing is CPU-bound and takes
tens of milliseconds, which is why it happens only on login and never on a hot
path. There is no synchronous I/O anywhere in a request path — the database
driver (`asyncpg`), the HTTP client (`httpx`), and the Redis client are all async.

---

## 6. Failure modes and expected behaviour

| What fails | Behaviour | Status |
|---|---|---|
| Redis unreachable at boot | Falls back to in-process cache + event bus; logged loudly; readiness reports `degraded` | 200 |
| Redis unreachable mid-request (rate limiter) | Fails **open** — a limiter that takes the API down has caused a worse outage than the one it prevents | 200 |
| Nominatim 403 / timeout | Falls back to the bundled gazetteer; `location_source` records which was used | 201 |
| No `ANTHROPIC_API_KEY` | LLM path never attempted; rule-based extractor used | 201 |
| Nothing resolvable from the description | Incident created with `location.point = null`, `source = "unresolved"` | 201 |
| One report provider down | Other provider's reports returned, `degraded: true`, partial result cached 60s | 200 |
| Both report providers down, cache warm | Stale payload served, `X-Cache: STALE`, `sources` from live breaker state | 200 |
| Both report providers down, cache cold | The one genuine failure case | 502 |
| Provider returns a 200 with a malformed body | Treated as `PermanentError` (a contract break, not a blip); not retried | 502 |
| A single malformed item inside a valid payload | That item is dropped and counted; the rest are served | 200 |
| PostgreSQL unreachable | Readiness fails so the instance is pulled from rotation | 503 |
| arq worker not running | Caches simply fill on demand via the normal miss path | 200 |

---

## 7. What would change at 100× scale

Honest notes rather than premature optimisation.

| Pressure | Change |
|---|---|
| Deep pagination | Keyset/cursor pagination instead of `OFFSET` |
| Breaker inconsistency across workers | Share breaker state in Redis |
| Rate-limit burst at window boundaries | Sliding window or token bucket |
| Report volume | Paginate the feed; persist reports and index them |
| Geospatial write volume | Partition `resources` by region; consider a coarser `ST_SnapToGrid` cache key for repeated nearby queries |
| Search quality | `tsvector` + `ts_rank` instead of trigram `ILIKE` |
| Operational blindness | Prometheus metrics + OpenTelemetry tracing (`request_id` already threads through everything) |
| Event delivery guarantees | Redis Streams with consumer groups instead of fire-and-forget pub/sub |
