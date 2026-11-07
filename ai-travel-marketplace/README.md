# AI Travel Marketplace

**Status: Phases 1–8 implemented; Docker-backed Compose verification and live OpenAI verification pending. An isolated local load measurement is recorded for Phase 8.**

An educational design for a Python-based travel marketplace with conventional
search and booking flows plus an AI travel assistant. The goal is to learn
distributed-systems and AI-application trade-offs through a staged, runnable
local laboratory, not to claim production readiness.

The attached brief asked that architecture be reviewed before implementation.
The revised architecture was approved, including separate processes from the
first milestone. ADRs record the accepted decisions and their trade-offs. This
repository is being built phase by phase rather than as one large code dump.

## Proposal at a glance

- **Language:** Python.
- **Initial API framework recommendation:** FastAPI with typed request/response
  models. See proposed ADR-001.
- **Target shape:** independently deployable services, each owning its data and
  invariants; initially operated locally with Docker Compose.
- **Transactional source of truth:** PostgreSQL.
- **Cross-service workflow:** persistent booking Saga orchestrated by the
  Booking Orchestrator; no distributed database transaction.
- **Async integration:** versioned domain events through a Kafka-compatible
  broker, with transactional outbox/inbox patterns.
- **Search:** denormalized, eventually consistent read models; search results
  never promise inventory.
- **AI:** a real, user-configured model provider behind a provider-neutral AI
  Gateway, retrieval-grounded assistant, and MCP tools that call domain APIs
  rather than databases.
- **Purchase safety:** explicit, authenticated customer confirmation before
  any purchase or other irreversible financial action.
- **Integrations and data:** deterministic simulated non-AI providers and seed
  data; no cloud resource creation or real travel/payment integrations. The
  user supplies an AI provider key locally to exercise the real assistant.

## Read the design and implementation notes

1. [Architecture and service boundaries](docs/01-architecture.md)
2. [Data ownership and event contracts](docs/02-data-and-events.md)
3. [Booking Saga, state, and recovery](docs/03-booking-saga.md)
4. [AI, RAG, MCP, and authorization](docs/04-ai-and-mcp.md)
5. [Accepted ADRs, repository layout, and roadmap](docs/05-decisions-and-roadmap.md)
6. [Phase 1 service contracts and verified results](docs/06-phase1-core-marketplace.md)
7. [Phase 2 holds, expiry, cancellation, and verified results](docs/07-phase2-inventory-correctness.md)
8. [Phase 3 durable booking workflow and verified results](docs/08-phase3-durable-booking.md)
9. [Phase 4 events, retries, and search projection](docs/09-phase4-events-projections.md)
10. [Phase 5 OpenSearch quality and freshness](docs/10-phase5-search-quality.md)
11. [Phase 6 AI Gateway and retrieval-augmented answers](docs/11-phase6-ai-foundation.md)
12. [Phase 7 assistant, conversation state, and MCP tools](docs/12-phase7-assistant-and-mcp.md)
13. [Phase 8 production-readiness experiments](docs/13-phase8-readiness-experiments.md)

## Scope and guardrails

The target system covers destination discovery, flight/hotel/activity search,
itinerary preparation, individual reservations, multi-service booking,
simulated payment, and an AI assistant. OpenSearch, Redpanda, and Qdrant are
implemented for local search and event delivery. A configured AI-provider key
is the only external credential needed for interactive AI use.

The initial learning sequence deliberately postpones infrastructure-heavy
features until their value is testable. No Kubernetes, cloud deployment, real
payments, or live travel-provider integrations are in scope for this local lab.
Dedicated logging, metrics, and tracing infrastructure are deferred.

## Local quick start

Requirements: Docker Desktop with Compose v2 for the full multi-process setup.
The user API and services are Python; development tests use the configured
Python interpreter and SQLite test databases. PostgreSQL is used by Compose.
Published Compose ports bind to loopback only; do not expose this development
stack directly to untrusted networks. The local OpenSearch node uses a 512 MiB Java heap. Qdrant persists its
rebuildable vector index in a named volume. Allow additional Docker memory for
the database, broker, OpenSearch, and Qdrant.

From PowerShell:

```powershell
Set-Location .\ai-travel-marketplace
Copy-Item .env.example .env
docker compose config
docker compose up --build -d
Invoke-RestMethod http://127.0.0.1:58080/health
```

`.env` is local-only and ignored by Git. The included JWT secret and empty
OpenAI-key setting are for local development. The AI Gateway works only when
you provide `OPENAI_API_KEY`; without it, AI requests return an explicit 503
while search and booking remain available.

### Run the web frontend

The React/TypeScript frontend is an independent Vite app in `frontend/`; the
Python services remain under `services/` and `packages/`. Start the backend
with Compose first, then open a second PowerShell window:

```powershell
Set-Location .\frontend
npm install
npm run dev
```

Open the local URL printed by Vite (normally `http://localhost:5173`). In
development, Vite forwards `/api` requests to the local gateway at
`http://127.0.0.1:58080`; set `API_PROXY_TARGET` if the gateway uses another
address. Production builds can be checked with `npm run build` and previewed
locally with `npm run preview`. The frontend is not currently a Compose service.
Bookings remain simulated and require a signed-in user and explicit confirmation.

### Try the API

The gateway exposes the destination, flight, hotel, activity, and identity
services. Search is public; reservation endpoints require a bearer token.
Creating a reservation temporarily holds inventory for 15 minutes. Confirm a
hold before its expiry, or cancel it to release the inventory.

```powershell
$base = 'http://127.0.0.1:58080'
$checkIn = (Get-Date).Date.AddDays(8).ToString('yyyy-MM-dd')
$checkOut = (Get-Date).Date.AddDays(10).ToString('yyyy-MM-dd')
$user = Invoke-RestMethod -Method Post "$base/api/identity/auth/register" `
  -ContentType 'application/json' `
  -Body (@{ email = 'traveler@example.test'; password = 'a-long-local-password'; display_name = 'Traveler' } | ConvertTo-Json)
$login = Invoke-RestMethod -Method Post "$base/api/identity/auth/login" `
  -ContentType 'application/json' `
  -Body (@{ email = 'traveler@example.test'; password = 'a-long-local-password' } | ConvertTo-Json)
$headers = @{ Authorization = "Bearer $($login.access_token)" }

Invoke-RestMethod "$base/api/destination/countries"
Invoke-RestMethod "$base/api/destination/cities?query=Tokyo"
Invoke-RestMethod "$base/api/flights/search?origin=TYO&destination=KYO"
Invoke-RestMethod "$base/api/hotels/search?city_code=TYO&check_in=$checkIn&check_out=$checkOut&guests=2"
Invoke-RestMethod "$base/api/activities/search?city_code=TYO"
Invoke-RestMethod "$base/api/search/search?query=Tokyo&kind=hotel&min_price=50&max_price=250&sort_by=price_asc"

$flightHold = Invoke-RestMethod -Method Post "$base/api/flights/reservations" `
  -Headers ($headers + @{ 'Idempotency-Key' = 'flight-demo-0001' }) `
  -ContentType 'application/json' `
  -Body (@{ offer_id = 'offer-001'; passengers = 1 } | ConvertTo-Json)

Invoke-RestMethod -Method Post "$base/api/flights/reservations/$($flightHold.id)/confirm" `
  -Headers $headers
```

The generated hotel dates work with a freshly seeded database. Availability is
created relative to the date each service database is first seeded, so a
persisted database may need dates within its existing inventory window. Repeat
the same reservation request with the same idempotency key to receive the
existing hold; using that key for a different request returns `409`. A hold
expires after 15 minutes unless confirmed. Delete a reservation to cancel it
and release its inventory:

```powershell
Invoke-RestMethod -Method Delete "$base/api/flights/reservations/$($flightHold.id)" `
  -Headers $headers
```

The local seed is deterministic for the data relationships and IDs; scheduled
availability is generated relative to the day the database is first seeded.
It contains 10 countries, 30 cities, 100 attractions, 50 hotels with two room
types each, 30 simulated airlines, 100 flight offers, and 100 activities.

### Run tests and stop services

```powershell
& 'C:/Users/gusta/AppData/Local/Python/bin/python.exe' -m pip install -e '.[dev]'
& 'C:/Users/gusta/AppData/Local/Python/bin/python.exe' -m pytest
docker compose down
```

`docker compose down` stops containers but preserves the PostgreSQL volume.
There is no production deployment or real supplier/payment integration in this
milestone. Phase 3 adds the Booking Orchestrator and simulated Payment Service
to the Compose stack.

### Confirm a multi-service booking

Creating a quote does not reserve anything. Review the quote, then explicitly
confirm it by calling the booking endpoint with an idempotency key. The
orchestrator responds with `202 Accepted` and a booking status; this local
workflow currently runs synchronously before returning.

```powershell
$quote = Invoke-RestMethod -Method Post "$base/api/booking/quotes" `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body (@{ flight = @{ offer_id = 'offer-001'; origin = 'TYO'; destination = 'KYO'; passengers = 1 } } | ConvertTo-Json)

$booking = Invoke-RestMethod -Method Post "$base/api/booking/bookings" `
  -Headers ($headers + @{ 'Idempotency-Key' = 'trip-confirmation-0001' }) `
  -ContentType 'application/json' `
  -Body (@{ quote_id = $quote.id } | ConvertTo-Json)

$booking
Invoke-RestMethod "$base/api/booking/bookings/$($booking.id)" -Headers $headers
```

Repeat the start request with the same authenticated user, quote, and
idempotency key to retrieve the same booking. If a payment outcome is
`payment_unknown`, do not resume or cancel it; reconcile first:

```powershell
Invoke-RestMethod -Method Post "$base/api/booking/bookings/$($booking.id)/reconcile" `
  -Headers $headers
```

The simulator supports `normal`, `decline_authorization`,
`unknown_authorization`, `unknown_capture`, and `fail_refund` modes through
`PAYMENT_SIMULATOR_MODE`. These deterministic modes exercise recovery behavior;
they do not model or certify an external payment provider.

### Search the event-driven projection

Catalog snapshots are published from the service-owned outboxes to Redpanda
and indexed asynchronously by the Search service. Search results are advisory;
transactional APIs still validate live inventory.

```powershell
Invoke-RestMethod "$base/api/search/search?query=Tokyo"
Invoke-RestMethod "$base/api/search/search?kind=flight&city_code=TYO"
Invoke-RestMethod "$base/api/search/search/status"
```

Rebuilding is an administrative action. Send the configured local
`SEARCH_ADMIN_TOKEN` as `X-Admin-Token`:

```powershell
$adminHeaders = @{ 'X-Admin-Token' = $env:SEARCH_ADMIN_TOKEN }
Invoke-RestMethod -Method Post "$base/api/search/search/rebuild" -Headers $adminHeaders
```

The Search status response reports processed event count, indexed document
count, dead-letter count, consumer connectivity, and broker lag. Events that
cannot be published or projected after bounded retries go to the
`marketplace.events.dlq.v1` topic. Redpanda exposes its local Kafka listener on
port 19092.

### Ask a grounded travel question

The Phase 6 AI endpoint uses OpenAI chat and embeddings with curated local
knowledge indexed in Qdrant. It requires the bearer token from login and does
not expose booking tools or perform purchases:

```powershell
Invoke-RestMethod -Method Post "$base/api/ai/chat" `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body (@{ question = 'What should I consider when planning a visit to Kyoto?' } | ConvertTo-Json)
```

The response contains the answer, source citations, retrieved chunk count, and
configured chat model. Set `OPENAI_API_KEY` in `.env`; model names and embedding
dimensions are configurable there. The first startup may take time to embed
the curated documents. Check `/api/ai/ready` before asking questions.

### Use the assistant and MCP tools

Phase 7 adds a persisted, user-scoped conversation and a typed MCP tool server.
The model may search the catalog, save/read a draft itinerary, prepare a quote,
and check a booking status. `start_booking` is not offered to the model. A
purchase can begin only through the separate authenticated confirmation
endpoint, using the exact phrase returned with a pending quote:

```powershell
$assistant = Invoke-RestMethod -Method Post "$base/api/ai/assistant/chat" `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body (@{ message = 'Find a flight from TYO to KYO and prepare a quote for offer-001' } | ConvertTo-Json)

$assistant
# If pending_quote is present, review its selection, amount, currency, and expiry.
$booking = Invoke-RestMethod -Method Post "$base/api/ai/assistant/confirm" `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body (@{
    quote_id = $assistant.pending_quote.quote_id
    confirmation_phrase = $assistant.pending_quote.confirmation_phrase
  } | ConvertTo-Json)
$booking
```

Send a later message with the returned `conversation_id` to continue the
conversation. Read or delete it through
`/api/ai/assistant/conversations/{conversation_id}`. Compatible MCP clients can
connect to `http://127.0.0.1:58080/api/ai/mcp` with the same bearer token.

The assistant service owns a separate `assistant` PostgreSQL database. The
Compose init script creates it for a fresh PostgreSQL volume. If upgrading an
existing local volume from Phase 6, create the database once before starting
the updated AI Gateway:

```powershell
docker compose exec postgres psql -U travel -d postgres -c "CREATE DATABASE assistant"
```

Assistant conversations with no activity for 90 days are deleted automatically
at AI Gateway startup and then daily. Users can also delete a conversation
immediately through the API. This policy does not delete itineraries or booking
records.

## Approved initial decisions

The user approved the proposal and its initial decisions. The most material
choices are:

- FastAPI and separate Python service processes from the first milestone.
- Redpanda as the local Kafka-compatible broker, while keeping event contracts
  Kafka-compatible.
- OpenSearch for marketplace search and Qdrant for vector retrieval.
- One local PostgreSQL server with a separately owned database per service as
  a development convenience; production deployment topology remains open.
- A real AI provider adapter for interactive testing, with its provider/model
  set to OpenAI first; the model defaults to `gpt-4.1-mini` and remains
  configurable. API keys stay in
  local environment configuration and out of source control.
- The first bounded contexts will run as separate service processes, rather
  than being combined into a modular monolith.

Non-AI third-party integrations remain simulated. No cloud resources are
created, and no real travel or payment transactions are made.
