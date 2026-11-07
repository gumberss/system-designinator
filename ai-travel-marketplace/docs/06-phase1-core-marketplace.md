# 6. Phase 1: core marketplace services

Phase 1 establishes independently running FastAPI processes, deterministic
local catalog data, identity, search, and direct reservations. It is an initial
learning milestone, not a production-ready distributed marketplace.

## Service contracts

The gateway accepts `/api/{service}/{path}` and forwards requests to the
configured service URL. It forwards authorization, content type, accept, and
idempotency-key headers. Search endpoints are public; reservation endpoints
require an authenticated bearer token.

| Service | Implemented endpoints |
|---|---|
| Gateway | `GET /health`; proxy routes under `/api/{service}/{path}` |
| Identity | `POST /auth/register`, `POST /auth/login`, `GET /users/me`, `PUT /users/me/preferences` |
| Destination | `GET /countries`, `GET /cities`, `GET /destinations/{city_code}`, `GET /destinations/{city_code}/attractions`, `GET /attractions` |
| Flight | `GET /airlines`, `GET /flights/search`, `POST /reservations` |
| Hotel | `GET /hotels/search`, `POST /reservations` |
| Activities | `GET /activities/search`, `POST /reservations` |

Reservation requests use an idempotency key. Replaying a request with the same
key and payload returns the original reservation; reusing the key with a
different payload returns `409`. Flight inventory is decremented conditionally
to prevent overselling. Hotel and activity inventory updates are performed by
their owning services.

Phase 2 extends the reservation endpoints into a hold lifecycle: a new
reservation is initially `held` for 15 minutes and must be confirmed or
cancelled. See [Phase 2](./07-phase2-inventory-correctness.md) for the current
behavior and verification.

## Seed data and persistence

The seed data includes 10 countries, 30 cities, 100 attractions, 50 hotels with
two room types each, 30 airlines, 100 flight offers, and 100 activities.
Identities and relationships are deterministic. Flight schedules and hotel and
activity availability are generated relative to the date their service database
is first seeded.

Compose configures separate PostgreSQL databases for the services. The tests
use isolated SQLite databases, so passing those tests does not establish
PostgreSQL compatibility or end-to-end multi-container behavior.

## Verification

Verified locally:

- `python -m pytest -q`: 6 tests passed, including service seed and API checks,
  authentication, idempotent reservation retries, a conflicting idempotency
  key, concurrent flight inventory requests, and a mocked gateway proxy test.
- `docker compose config --quiet`: configuration parsed successfully.

The Docker Desktop Linux engine was unavailable in this environment, so
containers could not be started and gateway-to-service behavior over Compose
and PostgreSQL remains unverified. The gateway test uses a mocked upstream; it
does not replace that runtime check.

## Explicitly deferred

Phase 1 does not implement a booking orchestrator or Saga, payments, Kafka or
an outbox, search indexing, an AI Gateway, OpenAI calls, RAG, or MCP tools.
OpenAI is the planned first model provider, but Phase 1 does not require an API
key. Non-AI travel and payment integrations remain simulated.
