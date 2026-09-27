# Architecture notes

Extra detail that did not belong in the README: the request lifecycle, the
measured query plans behind the geospatial claims, and how failure handling
behaves.

---

## 1. Request lifecycle

What happens on `POST /api/v1/disasters`, end to end.

1. `RequestContextMiddleware` assigns or propagates `X-Request-ID`, starts a
   timer, and binds the request id into a `ContextVar` so every log line below
   carries it.
2. `CORSMiddleware` applies the origin policy.
3. `RateLimitMiddleware` does a Redis `INCR` on a key of bucket, client IP and
   time window. Over the limit it returns a 429 problem document and stops.
4. `SecurityHeadersMiddleware` adds nosniff, DENY, Referrer-Policy and a CSP.
5. FastAPI routes to `create_disaster` and Pydantic validates `DisasterCreate`.
   Anything invalid becomes a 422 problem document.
6. The `require_roles` dependency decodes the JWT, re-reads the user from the
   database, checks `is_active`, then checks the role.
7. The `get_session` dependency yields one `AsyncSession` for the request.
8. `disaster_service.create_disaster` runs. If coordinates were supplied the
   source is recorded as manual with confidence 1.0. Otherwise
   `geocoding_service.resolve_from_description` is called, which caches the
   extracted place name under `nlp:place:{hash}` and the geocoding result under
   `geocode:{place}`.
9. `disaster_repo.create` inserts the row with
   `ST_SetSRID(ST_MakePoint(lng, lat), 4326)`, and `audit_repo.record` adds the
   audit entry to the same transaction.
10. `session.commit()` makes the write durable.
11. `events.publish()` broadcasts to Redis pub/sub, which reaches every worker
    and from there every WebSocket and SSE client.
12. The response is a 201 with `X-Request-ID` and `X-Response-Time-ms`.

Two orderings in that flow are deliberate.

The commit happens before the publish. A client that receives `disaster_created`
and immediately issues a GET must not get a 404.

The circuit breaker sits outside the retry loop in the integration clients, so a
run of three retries against a dead provider counts as one failure against the
breaker rather than three. Inverted, a single request would burn three of a five
failure budget and trip the breaker almost immediately.

---

## 2. Measured query plans

Run against this schema with 200,000 resources on PostgreSQL 16 with PostGIS 3.4.
Both queries return identical rows. The SQL to reproduce it is in section 2.3.

### 2.1 ST_DWithin, the query the application issues

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
   Sort (actual time=6.373..6.376 rows=50 loops=1)
     Sort Method: quicksort  Memory: 37kB
     Bitmap Heap Scan on resources (actual time=5.893..6.332 rows=95 loops=1)
       Filter: (is_active AND st_dwithin(location, '...'::geography, '5000'::double precision, true))
       Rows Removed by Filter: 77
       Heap Blocks: exact=161
       Bitmap Index Scan on ix_resources_location (actual time=0.151..0.151 rows=172 loops=1)
         Index Cond: (location && _st_expand('...'::geography, '5000'::double precision))
         Buffers: shared hit=24
 Planning Time: 1.053 ms
 Execution Time: 6.473 ms
```

The line that matters is the index condition,
`location && _ST_Expand(point, 5000)`. The planner rewrote `ST_DWithin` into a
bounding box overlap that the GiST index can answer. It read 172 candidate rows
out of 200,000, then applied the exact spheroid distance to only those,
discarding 77 and keeping 95.

Those 77 discarded rows are worth noticing, because they are the proof that the
filter and refine split is happening. A bounding box is a square and the radius
is a circle, so the corners produce false positives that the exact phase removes.

### 2.2 ST_Distance < r, the naive equivalent

```sql
WHERE is_active
  AND ST_Distance(location, ST_SetSRID(ST_MakePoint(-74.0060,40.7128),4326)::geography) < 5000
```

```
 Limit (actual time=276.982..286.235 rows=50 loops=1)
   Buffers: shared hit=3948
   Gather Merge (actual time=209.399..218.648 rows=50 loops=1)
     Workers Planned: 2
     Workers Launched: 2
     Sort (actual time=190.753..190.755 rows=22 loops=3)
       Parallel Seq Scan on resources (actual time=82.102..190.602 rows=32 loops=3)
         Filter: (is_active AND (st_distance(location, '...'::geography, true) < '5000'::double precision))
         Rows Removed by Filter: 66646
         Buffers: shared hit=3874
 Planning Time: 0.105 ms
 Execution Time: 311.923 ms
```

A Parallel Seq Scan. `ST_Distance` has to be evaluated for every row before the
comparison can be made, so the index is unusable. Postgres threw two extra worker
processes at it and it was still around 48 times slower.

To summarise the two:

- `ST_DWithin` uses a Bitmap Index Scan into a Bitmap Heap Scan, examines 172
  rows, reads 257 buffers and takes 6.47 ms
- `ST_Distance < r` uses a Parallel Seq Scan with two workers, examines 200,000
  rows, reads 3,948 buffers and takes 311.92 ms

Same results, roughly 48 times faster and 15 times fewer buffer reads. This is
the clearest argument for the `ST_DWithin` plus GiST design.

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
-- run both EXPLAIN statements above

DELETE FROM resources WHERE name LIKE 'benchmark\_%';
ANALYZE resources;
```

### 2.4 Tag filtering

`?tag=flood` compiles to `tags @> ARRAY['flood']`, served by the GIN index
`ix_disasters_tags`. GIN builds an inverted index, one posting list per distinct
tag pointing at the rows carrying it, so this is a lookup rather than a scan over
every row's array.

Worth knowing: `'flood' = ANY(tags)` would not use that index. The containment
operator is what makes it work.

---

## 3. How the cache behaves

A read goes through these steps.

1. Check the in process L1 dict. A hit returns immediately as `X-Cache: HIT`.
2. Check Redis. If the stored entry is younger than its TTL it is returned, also
   as a hit.
3. Otherwise the entry is missing or soft expired, and the caller tries to take
   an atomic `SET NX EX` lock.
4. Whoever wins the lock calls the origin. On success the value is stored and
   returned as `X-Cache: MISS`. On failure, stale data is served as
   `X-Cache: STALE` with `degraded: true`, or a 502 if nothing is cached at all.
5. Callers that lose the lock serve stale data if there is any, and otherwise
   poll for up to two seconds for the winner's result before fetching themselves.

The Redis key's own expiry is the TTL plus the stale grace period, which is what
creates the window in which stale data remains available after the soft expiry
has passed. Key sizes and lifetimes live in `.env.example`, and the reasoning
behind each TTL is in the README.

---

## 4. How the circuit breaker behaves

There is one breaker per provider (`nominatim`, `anthropic`, `mock-social-api`,
`mock-official-api`), per process.

It starts closed and calls pass through. After five consecutive failures it opens
and rejects calls immediately without attempting them. Thirty seconds later it
moves to half open and allows exactly one trial call. If that succeeds it closes,
and if it fails it reopens and the timer restarts.

Four properties are easy to get wrong, and are tested in
`tests/test_cache_and_resilience.py`:

- It counts consecutive failures, not cumulative ones. Any success resets the
  counter, so isolated blips never accumulate towards tripping it.
- Half open admits exactly one probe. Letting full load through the moment the
  timer expires would flatten a service that is still fragile.
- A `PermanentError`, meaning a 4xx, never counts. That means our request was
  wrong, not that the dependency is down, and counting it would trip the breaker
  on our own bad input.
- State is read lazily, so the transition from open to half open happens on the
  next read of `.state` and there is no background timer task to manage.

Live state is exposed at `GET /api/v1/health/ready` under
`checks.external_providers`, which makes "which provider are we currently
skipping, and why" answerable at a glance.

---

## 5. Concurrency model

Everything is async, on one event loop per worker. Two places matter.

Providers are fetched concurrently:

```python
results = await asyncio.gather(social_task, official_task, return_exceptions=True)
```

Latency is that of the slowest provider rather than the sum of both.
`return_exceptions=True` is not optional here. Without it the first failure
cancels the sibling task and a working provider's data is thrown away with it.

Blocking work is kept off the loop. Argon2 hashing is CPU bound and takes tens of
milliseconds, which is why it happens only on login and never on a hot path.
There is no synchronous I/O anywhere in a request path, since asyncpg, httpx and
the Redis client are all async.

---

## 6. Failure modes and expected behaviour

- Redis unreachable at boot: falls back to the in process cache and event bus,
  logs it loudly, and readiness reports degraded. Still returns 200.
- Redis unreachable mid request in the rate limiter: fails open, because a
  limiter that takes the API down has caused a worse outage than the one it was
  preventing. Still returns 200.
- Nominatim returns 403 or times out: falls back to the bundled gazetteer, and
  `location_source` records which path was used. Still returns 201.
- No `ANTHROPIC_API_KEY`: the LLM path is never attempted and the rule based
  extractor is used. Still returns 201.
- Nothing resolvable from the description: the incident is created with
  `location.point` null and the source recorded as unresolved. Still returns 201.
- One report provider down: the other provider's reports are returned with
  `degraded: true`, and the partial result is cached for only 60 seconds so
  recovery is picked up quickly. Returns 200.
- Both report providers down with a warm cache: the stale payload is served with
  `X-Cache: STALE` and `sources` taken from live breaker state. Returns 200.
- Both report providers down with a cold cache: the one genuine failure case.
  Returns 502.
- A provider returns a 200 with a malformed body: treated as a permanent error,
  since it is a contract break rather than a blip, and is not retried. Returns
  502.
- A single malformed item inside an otherwise valid payload: that item is dropped
  and counted, and the rest are served. Returns 200.
- PostgreSQL unreachable: readiness fails so the instance is pulled out of
  rotation. Returns 503.
- The arq worker is not running: caches simply fill on demand through the normal
  miss path. Returns 200.

---

## 7. What would change at 100x scale

Honest notes rather than premature optimisation.

- Deep pagination would move from `OFFSET` to keyset or cursor pagination.
- Breaker inconsistency across workers would be solved by sharing breaker state
  in Redis.
- Rate limit bursts at window boundaries would need a sliding window or a token
  bucket.
- Report volume would need the feed paginated, and reports persisted and indexed.
- Geospatial write volume would suggest partitioning `resources` by region, and
  possibly a coarser `ST_SnapToGrid` cache key so repeated nearby queries share
  an entry.
- Search quality would move from trigram ILIKE to `tsvector` with `ts_rank`.
- Operational blindness would be addressed with Prometheus metrics and
  OpenTelemetry tracing. `request_id` already threads through everything, so
  traces would slot in cleanly.
- Event delivery guarantees would move from fire and forget pub/sub to Redis
  Streams with consumer groups.
