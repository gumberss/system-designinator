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


### AI Concierge: From Trip Planning to Booking Confirmation

#### 1. Request a Travel Itinerary
<img width="516" height="757" alt="image" src="https://github.com/user-attachments/assets/a00a74a8-214a-4782-8d49-159c0614eaa3" />

#### 2. Provide Additional Trip Details

<img width="523" height="779" alt="image" src="https://github.com/user-attachments/assets/f38def04-dfe7-4b95-8ede-832f794c9e53" />
#### 3. Review the Trip Proposal

<img width="526" height="737" alt="image" src="https://github.com/user-attachments/assets/c72d4794-1566-49e0-b000-6aa8e6ac0e26" />


#### 4. Confirm Your Booking

<img width="519" height="948" alt="image" src="https://github.com/user-attachments/assets/3933127c-359b-40e2-ad16-a353668ba28c" />