# 2. Data ownership and event contracts

**Status: approved architecture; Phases 4–5 implement the event envelope,
outbox/inbox, Redpanda transport, catalog projection, and OpenSearch index.**

## Initial data ownership matrix

“Database per service” means independent ownership and access boundaries. A
single PostgreSQL process in a laptop is only a local deployment shortcut; it
does not make the databases shared. Each service should use its own database
role and migration path.

| Owner | Primary records | Important invariants / notes | Proposed store |
|---|---|---|---|
| Identity | User, profile, preferences, roles, consent metadata | Password/credential handling is isolated; travel preferences are user-owned | PostgreSQL |
| Destination | Country, region, city, attraction, airport reference, descriptions | Stable IDs; catalog relationships enforced at owner | PostgreSQL |
| Flight | Airline, route, schedule, fare, inventory, hold, reservation, ticket state | Atomic capacity decrement/hold and idempotent reservation operations | PostgreSQL |
| Hotel | Hotel, room type, date inventory, hold, reservation | Capacity per room type/date never becomes negative or oversold | PostgreSQL |
| Activities | Activity, schedule, capacity, hold, reservation | Capacity per schedule is atomically enforced | PostgreSQL |
| Search | Denormalized destination/flight/hotel/activity documents and projection offsets | Rebuildable, possibly stale, not authoritative | OpenSearch |
| Booking Orchestrator | Booking, immutable item/price snapshot, Saga step, command/result IDs, retries | Persistent state machine and idempotency across restart | PostgreSQL |
| Payment | Payment intent, authorization, capture, void, refund, provider reference | Unique idempotency key per financial operation; redact sensitive provider data | PostgreSQL |
| RAG / Knowledge | Source metadata, chunk metadata, embedding references, ingestion state | Rebuildable derived content; source/version attribution required | PostgreSQL metadata + Qdrant |
| AI Travel Assistant | Conversation/session and user-approved preference facts | Retention and deletion policy required; do not store secrets in prompts | PostgreSQL or bounded session store |
| MCP / Gateway | No business ownership | Do not duplicate transactional records | No separate persistence; dedicated audit/telemetry infrastructure is deferred |

Itineraries should initially be owned by the Booking Orchestrator (draft and
booking snapshot) or by a dedicated itinerary module inside that service.
Create a separate Itinerary Service only if independent lifecycle, volume, or
team ownership justifies the extra boundary. This avoids inventing a service
without a demonstrated consistency need.

Search documents may copy fields from multiple services, but each copied field
is a projection with an owning source and event version. No service is allowed
to update another service's records or schema directly.

## Domain event catalog (initial)

Events are facts that have already committed in their owning service. Each
event should carry an event ID, event type and schema version, aggregate ID and
version, occurred-at timestamp, producer, causation ID, and minimal payload.
Avoid embedding sensitive user data or full payment details.

| Producer | Event | Projection / workflow consumers | Notes |
|---|---|---|---|
| Identity | `UserProfileUpdated` | Assistant preference projection | Version by user; minimize personal data |
| Destination | `DestinationPublished`, `DestinationUpdated` | Search, RAG | Search index can replay/rebuild |
| Flight | `FlightOfferUpdated` | Search | Does not guarantee live seat availability |
| Flight | `FlightReservationHeld`, `FlightReservationConfirmed`, `FlightReservationReleased`, `FlightReservationExpired` | Booking, Search | A hold is temporary; expiry is decided by Flight |
| Hotel | `HotelPublished`, `HotelInventoryChanged` | Search, RAG | Inventory projection is advisory |
| Hotel | `HotelInventoryHeld`, `HotelReservationConfirmed`, `HotelInventoryReleased`, `HotelHoldExpired` | Booking, Search | Include date range, room type, reservation ID and aggregate version |
| Activities | `ActivityPublished`, `ActivityScheduleChanged` | Search, RAG | Catalog and schedule changes |
| Activities | `ActivityCapacityHeld`, `ActivityReservationConfirmed`, `ActivityCapacityReleased`, `ActivityHoldExpired` | Booking, Search | Capacity is enforced by Activities service |
| Booking | `BookingStarted`, `BookingStepCompleted`, `BookingCompensationStarted`, `BookingCompleted`, `BookingFailed`, `BookingNeedsReconciliation` | Gateway, assistant, operations | Avoid leaking internal error or payment details |
| Payment | `PaymentAuthorizationSucceeded`, `PaymentAuthorizationDeclined`, `PaymentCaptureSucceeded`, `PaymentCaptureUnknown`, `PaymentVoided`, `PaymentRefunded` | Booking, operations | Unknown is a first-class state, not failure |
| Knowledge | `KnowledgeSourceIngested`, `KnowledgeIndexUpdated` | RAG operations | Optional operational events |

Names and payloads are provisional. Before implementation, define a schema
registry or checked-in versioned JSON Schema/Avro contracts, then add
compatibility tests. Do not silently rename or reinterpret a published event.

Phase 4 uses a checked-in envelope contract at
`packages/contracts/events/event-envelope.v1.schema.json`. Payload-specific
validation remains in each consumer. The first implemented events are catalog
snapshots, reservation state changes, booking state changes, and payment
operation outcomes; the remaining catalog in the table is a future contract.

## Transactional outbox

For each service that emits events, persist the business update and an outbox
row in one local database transaction:

```text
BEGIN
  validate and update owned aggregate
  INSERT outbox(event_id, aggregate_id, aggregate_version, type, payload, ...)
COMMIT

publisher reads committed outbox rows
publisher sends event to broker
publisher marks row published (or records delivery attempt)
```

This removes the dual-write gap where a database commit succeeds but a broker
publish fails, or a broker publish succeeds while the database transaction
rolls back. The publisher can crash after broker acceptance and before marking
the row, so duplicate publication remains possible. Consumers must deduplicate;
the outbox does not provide exactly-once effects.

## Inbox and idempotent consumers

An event consumer records `(consumer_name, event_id)` in an inbox table in the
same transaction as the consumer's projection/workflow update. A unique key
prevents the same event from applying twice after redelivery or restart.
External effects must also carry stable idempotency keys; a database inbox
cannot make an external payment provider exactly-once.

For APIs, use an idempotency record scoped to authenticated principal and
operation. Persist the request fingerprint and terminal result. Reusing the
same key with a different payload must fail explicitly rather than return an
unrelated booking.

## Ordering, partitioning, retries, and DLQ

- Partition events by aggregate ID where per-aggregate order matters.
- Aggregate versions allow consumers to detect duplicates, gaps, or stale
  updates and to request replay/reconciliation.
- Kafka-compatible brokers preserve order within a partition, not globally.
- Retrying a failed event can allow later events to overtake it if the consumer
  design moves work to a retry topic. Consumers must validate aggregate
  version/state instead of assuming global FIFO.
- Use bounded retries with exponential backoff and jitter; classify transient,
  permanent, and unknown outcomes.
- Dead-letter records retain the original event ID, schema version, failure
  metadata, and correlation context. DLQ replay must remain idempotent and
  auditable.
- Do not acknowledge an event before its inbox and state change commit.
- Keep consumer lag, retry age, DLQ count, outbox age, and projection lag
  inspectable; a dedicated metrics/dashboard stack is deferred.

## Event schema evolution

Use additive, backward-compatible changes where possible. Consumers should
ignore unknown optional fields and reject unsupported major schema versions
with a visible error path. Never repurpose a field or change units without a
new version. Include a migration/replay test before removing old fields.

## Search staleness contract

Search indexes catalog and inventory events asynchronously. A result is a
candidate, not a promise. Before a hold or booking, the corresponding
transactional service rechecks current price and capacity and returns a fresh
quote/hold outcome. The UI and assistant must handle “found in search, no longer
available” as a normal conflict, not a system corruption.
