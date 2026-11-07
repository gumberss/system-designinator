# Phase 7: Assistant, conversation state, and MCP tools

**Status: Implemented and covered by automated contract tests. Live OpenAI,
PostgreSQL Compose, and external MCP client behavior have not been exercised.**

## Scope and trust boundaries

The AI Gateway now provides a single assistant endpoint with user-scoped
conversation persistence and a Streamable HTTP MCP server. The MCP server
delegates catalog searches and booking operations to their owning services;
it stores only assistant conversations, itineraries, and confirmation state
in its own `assistant` database.

Available tools:

- `search_travel`: bounded search across destination, flight, hotel, and
  activity results. Search results are advisory, not inventory holds.
- `create_itinerary` / `get_itinerary`: save and retrieve a traveler's draft,
  scoped to the authenticated user.
- `create_quote`: asks the Booking Orchestrator for a current quote; it does
  not hold inventory or start a Saga.
- `get_booking_status`: retrieves a booking through the Booking Orchestrator,
  which enforces ownership.
- `start_booking`: available on the MCP server only after the user has
  separately confirmed that exact quote.

The model does **not** receive `start_booking` in its tool list. It cannot
confirm on a user's behalf or turn conversational text such as “yes” into
authorization. The authenticated user must call the separate confirmation
endpoint with the exact quote ID, amount, and currency phrase returned by the
assistant. The confirmation record is bound to the authenticated owner, quote
fingerprint, amount, currency, expiry, and a stable idempotency key. A retry
uses the same key; the Booking Orchestrator remains authoritative for quote
validity and Saga execution.

The assistant never accesses service databases. MCP calls forward the
authenticated user's bearer token to platform APIs. MCP requests are also
authenticated at the AI Gateway. Conversation and itinerary reads are
user-scoped, and conversations can be deleted by their owner.

## API

`POST /api/ai/assistant/chat` accepts a message and optionally a conversation
ID:

```json
{
  "message": "Search for places to visit in Tokyo",
  "conversation_id": null
}
```

The response contains the conversation ID, assistant answer, retrieved source
citations, configured model, and a `pending_quote` when a quote was prepared.
That quote includes the exact confirmation phrase. Confirmation is a separate
authenticated request:

```json
{
  "quote_id": "quote-id-from-pending-quote",
  "confirmation_phrase": "CONFIRM quote-id-from-pending-quote 125.00 USD"
}
```

Send it to `POST /api/ai/assistant/confirm`. A changed, expired, unknown, or
other user's quote is rejected. Retries for an already-started booking return
the same booking result.

`GET /api/ai/assistant/conversations/{conversation_id}` reads the caller's
conversation, and `DELETE` removes it. `GET /api/ai/mcp` is the MCP
Streamable HTTP endpoint for compatible authenticated clients; the gateway
forwards MCP protocol headers and the bearer token.

## Local configuration

The AI Gateway uses `DATABASE_URL` for the separate assistant store. Compose
sets it to PostgreSQL database `assistant`, created by
`infra/postgres/init/001-databases.sql` for a fresh database volume. For an
existing Phase 6 volume, create that database once before starting the updated
stack:

```powershell
docker compose exec postgres psql -U travel -d postgres -c "CREATE DATABASE assistant"
```

OpenAI remains optional for running the rest of the marketplace. The assistant
requires the user-configured `OPENAI_API_KEY` and a ready Qdrant index. MCP
search and booking tools use the Compose `SEARCH_URL` and `BOOKING_URL`
service routes.

## Verification and limitations

`tests/test_phase7_assistant.py` verifies model tool selection, that the model
cannot see `start_booking`, conversation ownership, explicit confirmation
matching, and idempotent booking retries. `tests/test_gateway.py` verifies MCP
protocol-header forwarding. The broader suite also exercises the Booking
Orchestrator quote contract.

These tests use deterministic mocks for OpenAI and platform service calls.
They do not prove live model behavior, a real MCP client session through the
running Compose gateway, PostgreSQL runtime behavior, or a live booking
workflow. No real supplier or payment integrations are used.
