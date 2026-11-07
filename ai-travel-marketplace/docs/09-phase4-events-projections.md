# Phase 4: Events and search projection

**Status: Implemented and covered by SQLite-backed tests. The Docker,
PostgreSQL, and Redpanda runtime has not been exercised in this environment.**

Phase 4's SQL projection is the durable, rebuildable handoff to the OpenSearch
index added in Phase 5. Search requests use OpenSearch; the SQL tables remain
the inbox, recovery source, and durable indexing queue.

## Architecture

- Each event-producing service stores its outbox table in its own database.
  Business changes and reservation/booking/payment events are persisted in
  the same SQLAlchemy transaction.
- A background publisher sends at-least-once messages to Redpanda topic
  `marketplace.events.v1`, keyed by aggregate ID. After five failed sends, it
  attempts to forward the event and failure detail to
  `marketplace.events.dlq.v1`.
- The Search consumer commits Kafka offsets only after its inbox, aggregate
  cursor, and projection update commit. Duplicate event IDs are idempotent;
  stale aggregate versions are ignored and version gaps are retried then sent
  to the DLQ.
- Catalog bootstrap events are generated from each service's current local
  data, including existing databases upgraded from earlier phases. They
  include destination/attraction, flight, hotel, and activity documents.
- Reservation, booking, and payment state events are retained in the Search
  inbox; reservation state is also projected for visibility. Payment event
  payloads omit amounts and provider credentials.
- Search results are explicitly advisory; the transactional reservation and
  booking APIs remain the authority for price and capacity.

The event envelope is defined by the Pydantic model
`packages/common/events.py` and documented as JSON Schema at
`packages/contracts/events/event-envelope.v1.schema.json`. Payloads are
versioned independently with additive evolution expected.

## Search API

Through the Gateway:

- `GET /api/search/search?query=Tokyo&kind=destination&city_code=TYO&limit=20`
- `GET /api/search/search/status`
- `POST /api/search/search/rebuild` with the configured `X-Admin-Token`

Rebuild replays the durable Search inbox into the SQL projection in one
database transaction and queues the documents for OpenSearch reindexing. It
does not re-read transactional source data.

## Local operations and limitations

`docker compose up --build -d` now starts Redpanda, the source-service event
publishers, and Search. Redpanda is exposed locally on port 19092. The Search
status endpoint reports the active consumer connection, measured consumer
lag, processed event/document counts, and dead-letter count.

The event transport and Search process are real Kafka-compatible clients, not
in-memory mocks. Tests directly exercise the envelope, transactionally stored
outbox, inbox deduplication, version checks, projection, and rebuild using
SQLite; they do not establish PostgreSQL locking behavior, broker delivery
behavior, or Docker networking. Production-grade schema registry, event
retention, DLQ replay tooling, and observability infrastructure remain out of
scope.
