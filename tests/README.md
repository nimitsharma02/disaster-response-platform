# Tests

108 tests. All of them run offline, with every external HTTP call intercepted by
`httpx.MockTransport`, so nothing here touches the network.

Run them with:

    docker compose exec api pytest
    docker compose exec api pytest --cov=app
    docker compose exec api pytest -k geospatial

What each file covers:

- `test_disasters_api.py` - create, read, list, patch, tag filtering, pagination
  and the audit trail
- `test_validation_errors.py` - the error contract, meaning the 422, 404 and 401
  shapes, blank input, unknown fields and bad enum values
- `test_authz_rules.py` - the ownership rule, role gates, deactivated users, and
  expired and forged tokens
- `test_geospatial.py` - radius boundaries, distance accuracy, result ordering
  and the antimeridian case
- `test_cache_and_resilience.py` - cache hit, miss and stale paths, single
  flight, retry, the circuit breaker and the rate limiter
- `test_reports_integration.py` - provider normalisation, caching, and partial
  and total upstream failure
- `test_classification.py` - the priority scorer and place extraction

Two notes on how they work. The cache tests count how many times the mock
transport was actually called rather than trusting the `X-Cache` header, because
the header could be wrong and the call count cannot. And the geospatial tests run
against real PostGIS rather than a mock, since the whole claim being tested is
that `ST_DWithin` returns the right rows at the right distances.

Isolation comes from `conftest.py`. Each test runs inside a transaction that is
rolled back afterwards, using `join_transaction_mode="create_savepoint"` so the
application's own `commit()` calls still execute properly. Module level state
such as the cache and the circuit breakers is reset around every test, which is
what keeps the suite order independent. The suite uses its own
`disaster_response_test` database, created when the database container first
starts.
