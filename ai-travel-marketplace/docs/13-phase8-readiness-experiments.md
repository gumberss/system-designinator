# Phase 8: Production-readiness experiments

**Status: Implemented. Compose-backed measurements remain pending because the
Docker Engine is unavailable in the current environment.**

## Scope and guardrails

This phase adds a bounded, read-only HTTP load runner, gateway fault-injection
tests, a 90-day idle-conversation retention job, and a scoped security review.
It does not add dedicated logs, metrics, tracing, dashboards, production
deployment, or real supplier/payment integrations.

## Run a load experiment

Start the local Compose stack, then run the read-only Activities search workload
through the Gateway:

```powershell
Set-Location .\ai-travel-marketplace
& 'C:/Users/gusta/AppData/Local/Python/bin/python.exe' .\scripts\phase8_load.py `
  --url 'http://127.0.0.1:58080/api/activities/search?city_code=TYO' `
  --requests 200 `
  --concurrency 10
```

The script reports request count, concurrency, elapsed time, throughput,
successful and failed responses, status counts, transport errors, and p50/p95/
p99/max request latency. Use only a local or explicitly authorized target.
These numbers describe that exact workload and environment; they are not a
capacity guarantee. The default profile is intentionally bounded.

## Fault experiments

`tests/test_phase8_readiness.py` injects upstream timeout and connection
failures into the Gateway transport and verifies explicit 504/503 responses
without exposing transport details. Existing Phase 3 tests exercise booking
timeouts, retries, partial failures, and compensation; Phase 4 tests exercise
outbox retry and dead-letter behavior.

## Conversation retention

The AI Gateway deletes conversations whose `updated_at` is at least 90 days
old, once on startup and once per day thereafter. Activity extends retention.
The cleanup uses a bounded indexed timestamp predicate and logs the deleted
row count; database errors are logged and retried at the next scheduled run.
This policy covers assistant conversation history only; itineraries and booking
records are not deleted by it. Users can continue to delete individual
conversations immediately.

## Security review

Review scope: local Compose exposure, Gateway header forwarding and upstream
error handling, JWT validation, MCP authentication, and ownership checks on
assistant conversation/quote operations.

| Area | Finding | Action / limitation |
|---|---|---|
| Compose host bindings | PostgreSQL, Redpanda, OpenSearch (security plugin disabled), Qdrant, and the Gateway were published on all host interfaces. The data services do not provide production-grade authentication in this local profile. | All published ports now bind to `127.0.0.1`. Do not change them to public interfaces without adding service authentication, network controls, and TLS. |
| Authentication | Domain APIs and MCP validate bearer JWTs; JWT signing uses a configured secret and HS256. The Gateway forwards only an explicit header allowlist, including MCP protocol headers. | No authentication bypass was identified in the reviewed paths. The Compose credentials are development defaults, not production secrets. |
| Ownership | Conversation and quote lookups filter by authenticated user; MCP tools use the authenticated caller context. | Ownership behavior is covered by Phase 7 tests. |
| Production boundary | The local stack uses plaintext HTTP, development defaults, and no dedicated rate limiting, secret manager, or production observability. | Production deployment remains out of scope; those controls must be designed before public exposure. |

This is a scoped review of the listed paths, not a penetration test or a claim
that every dependency, deployment environment, or attack class has been
assessed.

## Verification state

- Automated fault-injection and retention tests are in
  `tests/test_phase8_readiness.py`.
- Compose configuration can be checked with `docker compose config`.
- An isolated local benchmark was run against one Uvicorn Activities process
  using a temporary SQLite database: Windows, Python 3.14.5, 200 read-only
  `GET /activities/search?city_code=TYO` requests, concurrency 10. It returned
  200/200 HTTP 200 responses with no transport errors; elapsed time was 4.187s,
  throughput 47.77 requests/second, p50 150.77ms, p95 201.13ms, p99 224.42ms,
  and max 980.57ms. CPU/memory and hardware details were not captured, so this
  is a local baseline only—not a capacity claim.
- Docker-backed Compose benchmarking remains pending because the Docker Engine
  was unavailable in the current environment. No Compose, PostgreSQL, broker,
  or OpenSearch runtime result is claimed here.
