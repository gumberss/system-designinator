# 5. Accepted ADRs, repository layout, and roadmap

**Status: architecture approved for phased implementation.**

## Initial Architecture Decision Records

The following decisions were accepted in the architecture review. Revisit them
only through an explicit ADR update when implementation evidence or a changed
requirement justifies doing so.

### ADR-001 — Python service framework

- **Status:** Accepted.
- **Context / problem:** The user requires Python; the project needs typed HTTP
  contracts, request validation, and OpenAPI documentation.
- **Decision:** Recommend FastAPI with Pydantic request/response models and
  async APIs where I/O concurrency benefits. Keep domain logic independent of
  the HTTP framework.
- **Alternatives:** Django REST Framework (integrated admin and broader
  batteries), Flask (smaller but more extension assembly), a standard-library
  service (too much custom infrastructure for this learning goal).
- **Advantages:** Validation and API schemas are explicit; easy to expose
  service contracts and run focused tests.
- **Disadvantages / trade-offs:** Async behavior adds complexity and does not
  make CPU-heavy work faster; dependency and lifecycle patterns require care.
- **Operational consequences:** Pin a supported Python version; use health
  endpoints, bounded connection pools, and per-service settings.
- **Reconsider when:** A framework's operational overhead or team needs show
  material drawbacks, or another Python runtime/framework is required.

### ADR-002 — Service-owned data and PostgreSQL

- **Status:** Accepted.
- **Context / problem:** Inventory and booking operations need transactional
  invariants; service boundaries must prevent cross-domain writes.
- **Decision:** PostgreSQL per service as the transactional system of record.
  Run one PostgreSQL server locally with separate databases and roles as a
  resource-saving convenience.
- **Alternatives:** One shared schema/database with logical ownership; a
  separate server/container per service; NoSQL for selected services.
- **Advantages:** ACID transactions, constraints, mature tooling, and a clear
  ownership boundary without requiring many laptop-sized database instances.
- **Disadvantages / trade-offs:** Multiple databases still add migrations,
  pools, and operational work; a shared local server is not production
  isolation.
- **Operational consequences:** Separate credentials/migrations; no
  cross-service SQL; account for summed pool limits; document restore/replay.
- **Reconsider when:** A service needs a different storage model or isolation
  requirement is not met.

### ADR-003 — Kafka-compatible events and Redpanda local profile

- **Status:** Accepted.
- **Context / problem:** Durable asynchronous projections and workflows need
  partitioned event streams, consumer groups, and replay.
- **Decision:** Use Kafka-compatible contracts and recommend Redpanda for the
  local Compose broker. Preserve the option to use Apache Kafka later.
- **Alternatives:** Apache Kafka, RabbitMQ, PostgreSQL outbox polling without a
  broker in the first milestone, or synchronous HTTP only.
- **Advantages:** Partition-based ordering, replay, and scalable consumers
  match the event/search learning goals; Redpanda may be simpler locally.
- **Disadvantages / trade-offs:** Broker operations are a meaningful local
  resource cost; Kafka semantics do not provide global ordering or
  exactly-once business effects.
- **Operational consequences:** Outbox/inbox, schema evolution, partition keys,
  retry and DLQ policy, lag monitoring, and broker recovery tests are required.
- **Reconsider when:** Local resource constraints, team familiarity, or actual
  messaging semantics favor another broker.

### ADR-004 — CQRS search projection with OpenSearch

- **Status:** Accepted.
- **Context / problem:** Search filtering/ranking and transactional inventory
  have different read and consistency needs.
- **Decision:** Use OpenSearch for denormalized marketplace read models,
  asynchronously projected from versioned events; revalidate before a hold.
- **Alternatives:** PostgreSQL full-text search, Elasticsearch, direct reads
  from transactional services, or a dedicated managed search service.
- **Advantages:** Search can evolve/query independently and tolerate stale
  projections without weakening booking correctness.
- **Disadvantages / trade-offs:** Index lag, duplicate data, schema migrations,
  and rebuild/reconciliation machinery.
- **Operational consequences:** Track projection checkpoints and lag; support
  reindexing; clearly label availability results as advisory.
- **Reconsider when:** Data volume and relevance needs do not justify operating
  a separate search engine.

### ADR-005 — Orchestrated Saga for complete booking

- **Status:** Accepted.
- **Context / problem:** Flight, hotel, activities, and payment are owned by
  separate services and cannot share one local ACID transaction.
- **Decision:** Booking Orchestrator persists workflow state and coordinates
  holds, payment, confirmation, compensation, and reconciliation.
- **Alternatives:** Choreographed Saga, distributed transaction/2PC, or a
  synchronous best-effort chain with no durable state.
- **Advantages:** Central workflow visibility, explicit failure handling,
  durable recovery, and a natural place for booking status.
- **Disadvantages / trade-offs:** Orchestrator complexity and the risk of a
  central coordination bottleneck; compensation is not rollback.
- **Operational consequences:** Persist each transition, make steps idempotent,
  expose reconciliation states, and alert on stuck Sagas.
- **Reconsider when:** Workflow ownership becomes genuinely decentralized or
  the orchestration service's scale/availability characteristics require
  partitioning.

### ADR-006 — AI Gateway and provider-neutral contracts

- **Status:** Accepted; OpenAI selected as the first provider.
- **Context / problem:** Business services should not depend directly on one
  provider SDK or its response/error format.
- **Decision:** Put chat and embedding interfaces behind an AI Gateway and use
  OpenAI for the first interactive provider adapter. Keep model names
  configurable and a deterministic fake adapter available only for automated
  tests.
- **Alternatives:** Direct provider calls from the assistant; a commercial
  model gateway; one provider-specific application.
- **Advantages:** Testability, provider isolation, consistent timeout policy,
  and a real AI experience without coupling business services to one SDK.
- **Disadvantages / trade-offs:** Abstraction can hide provider-specific
  capabilities and adds an internal interface that must remain small.
- **Operational consequences:** Explicit provider/model configuration, an API
  key supplied through local environment configuration and excluded from
  source control, bounded retries, and visible degraded-mode errors.
- **Reconsider when:** Provider portability is not valuable or normalization
  prevents using a required capability safely.

### ADR-007 — Qdrant for RAG vector retrieval

- **Status:** Accepted.
- **Context / problem:** Travel knowledge needs semantic retrieval with
  metadata filters and source traceability.
- **Decision:** Recommend Qdrant for the vector index, with source documents
  and ingestion state retained separately and the index rebuildable.
- **Alternatives:** OpenSearch vector search, PostgreSQL with pgvector, or no
  vector store until evaluation justifies RAG.
- **Advantages:** A focused vector retrieval component and an explicit
  operational learning surface.
- **Disadvantages / trade-offs:** Another stateful dependency and possible
  duplication with search infrastructure.
- **Operational consequences:** Version embeddings, retain source references,
  and test reindexing, retrieval quality, and provider failure.
- **Reconsider when:** The corpus is small or a unified PostgreSQL/OpenSearch
  deployment meets tested quality and operational constraints.

### ADR-008 — MCP as an AI tool protocol boundary

- **Status:** Accepted.
- **Context / problem:** The assistant needs discoverable, typed tools while
  preserving service authority and the orchestrator's Saga ownership.
- **Decision:** Expose search, itinerary, and booking operations through an MCP
  Server that delegates to authenticated domain APIs.
- **Alternatives:** Provider-specific function calling only; direct assistant
  HTTP calls; tools implemented inside the orchestrator.
- **Advantages:** Standardized tool discovery/invocation and a separately
  testable tool boundary.
- **Disadvantages / trade-offs:** Protocol support does not replace
  authorization, confirmation, idempotency, or business validation.
- **Operational consequences:** Authenticate/authorize every call, propagate
  principal context, validate schemas, bound timeouts, audit side effects, and
  require explicit confirmation.
- **Reconsider when:** MCP's client compatibility or security model does not
  fit the selected runtime.

### ADR-009 — Explicit confirmation for purchases

- **Status:** Accepted.
- **Context / problem:** Model-generated tool calls must not autonomously
  trigger irreversible financial operations.
- **Decision:** Require server-verified confirmation bound to authenticated
  user, quote fingerprint, amount/currency, expiry, and idempotency key.
- **Alternatives:** Trust conversational intent; use only a generic “yes” turn;
  allow silent booking after itinerary generation.
- **Advantages:** Clear authority boundary and resistance to prompt injection,
  stale quotes, and accidental charges.
- **Disadvantages / trade-offs:** Adds a confirmation step and can require a
  fresh quote after expiry.
- **Operational consequences:** Store confirmation state safely, reject changed
  quotes, audit the decision, and test authorization without an LLM.
- **Reconsider when:** Never remove the explicit-consent invariant; only
  reconsider the confirmation UX or cryptographic representation.

### ADR-010 — Compose-first, progressive infrastructure

- **Status:** Accepted.
- **Context / problem:** The project is for learning and should be runnable
  locally before adopting production orchestration complexity.
- **Decision:** Use Docker Compose and deterministic seed/simulation workflows;
  introduce broker, search, and vector dependencies by milestone. Dedicated
  observability infrastructure is out of current scope; Kubernetes remains a
  later design exercise.
- **Alternatives:** Start with Kubernetes; run all dependencies directly on
  the host; make every service depend on every infrastructure component from
  day one.
- **Advantages:** Faster feedback, lower setup cost, and infrastructure
  justified by a demonstrated concept.
- **Disadvantages / trade-offs:** Local topology cannot prove production
  scaling, failure domains, or managed-service behavior.
- **Operational consequences:** Pin images, isolate volumes/ports, provide
  health/readiness checks, and document safe stop/reset procedures.
- **Reconsider when:** The learning objective explicitly shifts to deployment,
  multi-node behavior, or orchestration.

### ADR-011 — Separate service processes from the first milestone

- **Status:** Accepted.
- **Context / problem:** The user wants to begin with the service boundaries
  already running independently, rather than first implementing a modular
  monolith and extracting services later.
- **Decision:** Run the Phase 1 gateway, identity, destination, flight, hotel,
  and activities services as separate processes/containers. Each domain
  service owns its database; the gateway routes requests and does not own
  domain logic.
- **Alternatives:** Begin as a modular monolith; combine related catalogs into
  one process; create every target service, including later AI and search
  services, immediately.
- **Advantages:** Service boundaries, internal APIs, process failures, and
  independent startup become tangible in the first runnable slice.
- **Disadvantages / trade-offs:** More local processes and configuration before
  the domain is complete; cross-service integration takes longer.
- **Operational consequences:** Compose must provide health checks, bounded
  startup dependencies, distinct ports, and per-service database URLs. The
  initial slice will not pretend that the later Search, Saga, or AI services
  already exist.
- **Reconsider when:** Local resource cost or implementation overhead blocks
  learning and a modular deployment is demonstrably more useful.

## Proposed repository structure

The tree is a future implementation outline; files are intentionally not
created until the corresponding milestone.

```text
ai-travel-marketplace/
  README.md
  pyproject.toml
  compose.yaml
  .env.example
  docs/
    01-architecture.md
    02-data-and-events.md
    03-booking-saga.md
    04-ai-and-mcp.md
    05-decisions-and-roadmap.md
    adr/
      ADR-001-python-service-framework.md
      ADR-002-service-data-ownership.md
      ...
  infra/
    postgres/
  services/
    gateway/
    identity/
    destination/
    flight/
    hotel/
    activities/
    search/                  # later milestone
    booking_orchestrator/    # later milestone
    payment/                 # later milestone
    ai_assistant/
    ai_gateway/
    mcp_server/
  packages/
    contracts/
    eventing/
  scripts/
    seed/
    experiments/
  tests/
    unit/
    integration/
    contracts/
    end_to_end/
    fault_injection/
```

Use a small number of shared packages for contracts and infrastructure
plumbing, not shared domain models that couple service releases. The actual
layout can be simplified if early milestones show independent deployables
create overhead without teaching value.

## Prioritized implementation roadmap

The architecture is the design baseline; the table below tracks implementation
progress and exit criteria.

| Phase | Goal and deliverable | Key proof / exit criteria | Status |
|---|---|---|---|
| 0. Review and foundation | Approved ADRs, define contracts/entities, Python layout, local tooling and test conventions | Architecture approved; contracts and local process layout established | Complete |
| 1. Core marketplace | Separate Gateway/API, Identity, Destination, Flight, Hotel, and Activities processes; deterministic seed data; basic search and reservation APIs | Unit/API tests; required seeded catalog counts | Implemented; Compose runtime unverified |
| 2. Inventory correctness | Hotel/activity/flight holds, idempotency, expiry, cancellation and concurrency handling | Concurrent tests prove capacity is never exceeded; key misuse is rejected | Implemented; PostgreSQL/Compose runtime unverified |
| 3. Durable booking workflow | Booking Orchestrator, payment simulator, Saga state, compensation and reconciliation | Restart/resume, duplicate-command, declined, timeout/unknown, and partial-failure tests | Implemented; SQLite tests pass; PostgreSQL/Compose runtime unverified |
| 4. Events and projections | Broker, transactional outbox/inbox, retries/DLQ, versioned events, Search projection | Duplicate/out-of-order event tests; rebuild and observable lag; stale search does not oversell | Implemented; SQLite tests and Compose config pass; Docker runtime unverified |
| 5. Search quality | OpenSearch indexing, filters, sort/rank, availability freshness semantics | Contract/integration tests; measured projection freshness under a declared local test | Implemented; unit/contract tests and Compose config pass; Docker runtime unverified |
| 6. AI foundation | AI Gateway, OpenAI chat/embedding adapter, RAG ingestion and Qdrant retrieval | Real interactive model calls with a user-supplied key; retrieval relevance/citation tests; graceful provider/vector-store failure | Implemented; automated contract tests pass; live OpenAI/Qdrant runtime unverified |
| 7. Assistant and MCP | Single assistant, conversation state, typed MCP tools, user-scoped access and explicit purchase confirmation | Tool-selection and authorization tests; booking without valid confirmation is impossible | Implemented; automated contract tests pass; live OpenAI/Compose runtime unverified |
| 8. Production-readiness experiments | Repeatable load/fault experiments, 90-day idle conversation retention, and a scoped security review; add observability only if later requested | Report environment, workload, p50/p95/p99, errors, throughput, and limits without claiming unmeasured scale | Implemented; Docker-backed benchmark pending |

Each milestone must include focused docs, a runnable local path, tests, and an
honest verified-results note. Do not create empty milestone files, claim a
benchmark that was not run, or treat a mock as proof of an external provider
guarantee.

## Review checklist before implementation

- Confirm FastAPI, service decomposition, and Python version target.
- Confirm the local broker and search/vector choices, or name alternatives.
- Phase 1 uses separate service processes from the start.
- Confirm the booking sequence and payment capture/compensation policy.
- OpenAI is selected as the first real AI provider; select the exact model
  during implementation. The API key is supplied locally and excluded from
  the repository; non-AI external integrations stay simulated.
- Confirm that dedicated logs, metrics, tracing, and dashboards stay deferred.
- Confirm explicit customer confirmation as a non-negotiable purchase rule.

The architecture review is complete. Implement the first agreed milestone,
then update the ADRs if implementation evidence changes an accepted decision.
