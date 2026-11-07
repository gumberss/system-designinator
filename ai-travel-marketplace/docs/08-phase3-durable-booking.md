# Phase 3: Durable booking workflow

**Status: Implemented and covered by SQLite-backed service tests. PostgreSQL
and full Docker Compose runtime have not been verified.**

## What is implemented

- Booking Orchestrator and local simulated Payment Service run as separate
  processes and have separate databases.
- `POST /quotes` creates an authenticated, ten-minute quote from the selected
  flight, hotel, and/or activity offers.
- `POST /bookings` is the explicit customer confirmation command. It requires
  an `Idempotency-Key` and persists the booking and stable Saga-step keys
  before calling inventory or payment services.
- `GET /bookings/{id}` returns status, quote total, reservation IDs, payment
  IDs, failure detail, and a version.
- `POST /bookings/{id}/resume` retries safe, durable workflow steps after an
  orchestrator interruption.
- `POST /bookings/{id}/reconcile` checks an unknown payment operation by its
  stable key. If the operation is not found, it retries with the same key.
- Successful reservations are held before payment authorization and capture,
  then confirmed. Definite failures compensate with inventory release plus a
  void or refund as applicable. Failed compensations are retained in
  `reconciliation_required` for retry rather than reported as clean rollback.
- Payment 5xx responses and timeouts are treated as unknown outcomes. Inventory
  remains held until reconciliation resolves the payment operation.
- Repeated start commands with the same user/key return the existing booking;
  reuse with a different quote is rejected.

## Payment simulator modes

Set `PAYMENT_SIMULATOR_MODE` in the environment or `.env`:

| Mode | Behavior |
|---|---|
| `normal` | Authorization and capture succeed. |
| `decline_authorization` | Authorization is declined; successful inventory holds are released. |
| `unknown_authorization` | Authorization is stored but initially reported as unknown. |
| `unknown_capture` | Capture is stored but initially reported as unknown. |
| `fail_refund` | Refund compensation fails and leaves the booking requiring reconciliation. |

These are deterministic test controls, not a model of real payment-provider
guarantees.

## Test evidence and limitations

`tests/test_phase3_booking.py` exercises successful/idempotent booking,
declined authorization and compensation, authorization reconciliation,
payment timeouts both before and after the simulated service persists an
operation, orchestrator interruption/resume, and compensation retry. The tests
use in-process FastAPI clients with isolated SQLite databases. They do not
prove PostgreSQL locking behavior, Docker networking, or real provider
semantics.

The PostgreSQL init scripts create the Booking and Payment databases only
when the data volume is initialized. A volume created before Phase 3 was added
will not rerun those scripts; create the two databases in that existing
instance before starting the new services.

## API usage

Booking endpoints are exposed through the Gateway under `/api/booking`.
Clients must authenticate, review the returned quote, then explicitly submit
its ID to `/bookings` with an idempotency key. When status is
`payment_unknown`, call `/bookings/{id}/reconcile`; do not treat it as a
decline or release inventory manually.
