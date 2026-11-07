# 7. Phase 2: inventory correctness

Phase 2 adds a temporary hold lifecycle to flight, hotel, and activity
reservations. The services still own inventory updates and enforce capacity
atomically. Search remains advisory; a successful hold is the authoritative
availability check.

## Reservation lifecycle

Creating a reservation with `POST /reservations` now creates a `held`
reservation and decrements inventory in the same service-owned transaction.
Holds expire 15 minutes after creation. A hold can be confirmed before expiry
with `POST /reservations/{id}/confirm`. Reservations can be cancelled with
`DELETE /reservations/{id}`; cancellation returns inventory exactly once.

The lifecycle states are:

- `held`: inventory is reserved until `expires_at`.
- `confirmed`: the hold was explicitly confirmed; inventory remains reserved.
- `cancelled`: inventory was released by cancellation.
- `expired`: the hold deadline passed and inventory was released.

Confirmation is idempotent for a confirmed reservation. Cancellation is
idempotent for cancelled or expired reservations. Both operations are scoped
to the authenticated owner. The existing idempotency key remains bound to the
same user and request, including after cancellation or expiry; retrying an
expired hold returns its terminal state rather than silently making a new hold.

The expiry sweep runs on inventory search, reservation creation, confirmation,
and cancellation requests. This avoids a separate worker in the current local
lab; it means an idle service may retain expired inventory rows until its next
request. The conditional state transition and inventory release share a
transaction so only one request can release a reservation.

## Capacity and persistence

- Flight seat decrement is a conditional database update.
- Hotel inventory is checked and decremented for every night as one
  transaction. Cancellation/expiry checks each date's total capacity before
  returning units.
- Activity capacity is decremented conditionally and returned with a bounded
  update.
- Reservation expiry timestamps are stored as timezone-naive UTC for
  consistent SQLite and PostgreSQL handling.
- Startup adds the nullable expiry column to existing service databases before
  seeding. Existing confirmed reservations remain confirmed.

## Verification

The local pytest suite has 9 tests passing, covering the earlier Phase 1
contracts plus:

- 15-minute expiry and inventory restoration for all three inventory types.
- Replaying an expired idempotency key without creating another reservation.
- Confirmation idempotency and rejection after expiry.
- Repeated cancellation without duplicate inventory release.
- Concurrent hotel and activity reservation attempts against finite capacity;
  exactly one succeeds in each test.

The tests use SQLite. Compose configuration parses, but the Docker engine was
not available for PostgreSQL and multi-container runtime verification. These
results therefore do not establish PostgreSQL concurrency behavior.

## Scope boundary

Phase 2 does not implement payment capture/refunds, multi-service booking,
Saga recovery, outbox events, or a background expiry worker. Those belong to
later roadmap phases. The 15-minute hold lifetime is a local-lab policy, not a
claim about supplier inventory guarantees.
