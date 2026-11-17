# System Flows

Numbered diagrams of the implemented request and background flows. Mermaid
diagrams render on GitHub and in compatible Markdown previews.

## Reading the diagrams

- Numbers show execution order within each diagram; numbering restarts for
  each flow. A loop repeats its enclosed steps.
- Solid arrows are calls or writes; dashed arrows are responses.
- Each PostgreSQL participant is a separate service-owned database. Compose
  runs these databases in one PostgreSQL container, not one shared schema.
- MCP, the assistant loop, and RAG run inside the AI Gateway process. They
  are shown separately where useful, but are not separate containers.
- Domain APIs validate bearer tokens locally; they do not call Identity for
  every request. Search is public; assistant and booking operations require
  authentication.
- Travel inventory and payments are simulated. Search results and quotes
  are not inventory guarantees.

## 1. Customer sign-in

```mermaid
sequenceDiagram
    participant C as Customer
    participant F as Frontend
    participant G as API Gateway
    participant I as Identity Service
    participant DB as PostgreSQL - identity
    C->>F: 1. Submit email and password
    F->>G: 2. POST /api/identity/auth/login
    G->>I: 3. POST /auth/login
    I->>DB: 4. Read user and password hash
    DB-->>I: 5. User record
    I->>I: 6. Verify password and sign JWT
    I-->>G: 7. Access token
    G-->>F: 8. Access token
    F-->>C: 9. Show signed-in interface
```

Registration follows the same routing through
`POST /api/identity/auth/register`, but writes a new account to the Identity
database instead of authenticating an existing one.

## 2. Marketplace search and direct catalog reads

```mermaid
sequenceDiagram
    participant C as Customer
    participant F as Frontend
    participant G as API Gateway
    participant S as Search Service
    participant OS as OpenSearch
    C->>F: 1. Search hotels in Tokyo
    F->>G: 2. GET /api/search/search?kind=hotel&city_code=TYO
    G->>S: 3. GET /search with filters
    S->>S: 4. Validate filters and build query and sort
    S->>OS: 5. POST /marketplace-catalog-v1/_search
    OS-->>S: 6. Matching documents and total
    S-->>G: 7. Items, total, sort and freshness
    G-->>F: 8. Search response
    F-->>C: 9. Display advisory results
```

The Search request reads OpenSearch, not the source-service PostgreSQL
databases. Its SQL database supports indexing and recovery, as shown in
flow 7.

`query` matches item text; `city_code` filters by location. To list Tokyo
hotels, use `kind=hotel&city_code=TYO`, without `query=Tokyo` unless that
additional text constraint is intentional. All filters are combined.

Direct catalog endpoints follow a different path, for example:

```mermaid
flowchart LR
    C[Customer / Frontend] -->|1. GET /api/destination/cities?query=Tokyo| G[API Gateway]
    G -->|2. GET /cities?query=Tokyo| D[Destination Service]
    D -->|3. Read city catalog| DB[(PostgreSQL - destination)]
    DB -->|4. City records| D
    D -->|5. Response via Gateway| C
```

Flight, Hotel, and Activities also expose domain search APIs backed by their
own databases. These differ from the denormalized Search Service index.

## 3. Concierge message: history, RAG, reasoning, and tools

```mermaid
sequenceDiagram
    participant C as Customer
    participant F as Frontend
    participant G as API Gateway
    participant AI as AI Gateway
    participant DB as PostgreSQL - assistant
    participant O as OpenAI
    participant Q as Qdrant
    participant M as Embedded MCP Server
    participant S as Search Service
    participant OS as OpenSearch
    C->>F: 1. Send concierge message
    F->>G: 2. POST /api/ai/assistant/chat + JWT
    G->>AI: 3. POST /assistant/chat
    AI->>AI: 4. Validate JWT and AI readiness
    AI->>DB: 5. Load owned conversation or create one
    DB-->>AI: 6. Conversation history
    AI->>O: 7. POST /v1/embeddings for current message
    O-->>AI: 8. Query vector
    AI->>Q: 9. Query travel-knowledge-v1
    Q-->>AI: 10. Relevant chunks with source metadata
    AI->>M: 11. List tools through in-process MCP client
    M-->>AI: 12. Schemas, excluding start_booking for the model
    AI->>AI: 13. Build instructions + history + RAG excerpts
    AI->>O: 14. POST /v1/responses with input and tools
    O-->>AI: 15. Output items and optional function calls
    opt Model requests search_travel
        AI->>M: 16. Call search_travel with model arguments
        M->>S: 17. GET /search with validated parameters
        S->>OS: 18. Query catalog index
        OS-->>S: 19. Matching documents
        S-->>M: 20. Search response
        M-->>AI: 21. Structured tool result
        AI->>O: 22. Output items + function_call_output
        O-->>AI: 23. Final answer or further tool requests
    end
    AI->>DB: 24. Save user message and final answer
    AI-->>G: 25. Answer, citations, conversation ID and pending quote
    G-->>F: 26. Assistant response
    F-->>C: 27. Display answer and optional confirmation UI
```

Important details:

- RAG runs before model inference on every message. Retrieval embeds the
  current message, not a history-based rewrite.
- The model chooses whether tools are necessary. Up to three tool rounds
  are allowed, with at most four calls per round. If all rounds are used,
  a final inference is made without tools.
- City-based searches resolve the catalog city code first:
  `search_travel(kind="destination", query="Tokyo")`, then
  `search_travel(kind="hotel", city_code="TYO", query="")`.
  This can span multiple model/tool rounds.
- Tool calls execute through an in-process MCP client. The internal
  assistant does not call the external `/api/ai/mcp` HTTP route.
- With a supported model, `OPENAI_REASONING_EFFORT=medium` explicitly enables
  reasoning. Responses use `store: false`; encrypted reasoning items, when
  returned, are passed back during tool rounds. They are not stored in the
  conversation database or exposed to the frontend.
- Only user and final assistant messages are persisted, capped at 40
  messages. Intermediate tool results are not saved as conversation history.

### Other tool destinations

| Tool | Execution path and storage |
|---|---|
| `search_travel` | MCP -> Search Service -> OpenSearch |
| `create_itinerary` / `get_itinerary` | MCP -> PostgreSQL `assistant`, scoped to the user |
| `create_quote` | MCP -> Booking Orchestrator -> domain APIs; quote in PostgreSQL `booking`, confirmation record in PostgreSQL `assistant` |
| `get_booking_status` | MCP -> Booking Orchestrator -> PostgreSQL `booking` |
| `start_booking` | Explicit confirmation flow only; see flow 5 |

Tools call domain APIs instead of reading other services' databases. The
assistant's itinerary and confirmation records are the exception only in the
sense that they belong to the AI Gateway itself.

### RAG-only endpoint

`POST /api/ai/chat` uses the same embedding and Qdrant retrieval path, then
calls Responses without tools or conversation history. It uses the read-only
system prompt from `build_grounded_messages`. The concierge instead supplies
its own tool-aware system prompt and reuses the retrieved excerpts.

## 4. Knowledge ingestion at AI Gateway startup

```mermaid
sequenceDiagram
    participant AI as AI Gateway background task
    participant K as Local knowledge Markdown files
    participant Q as Qdrant
    participant O as OpenAI Embeddings
    AI->>Q: 1. Ensure collection exists and dimensions match
    AI->>K: 2. Read documents
    K-->>AI: 3. Source text and metadata
    AI->>AI: 4. Compute revisions and overlapping chunks
    AI->>Q: 5. Count points matching corpus_revision
    Q-->>AI: 6. Exact count
    alt Count matches expected chunks
        AI->>AI: 7. Reuse existing index
    else Count differs
        AI->>O: 7. Embed chunks in batches
        O-->>AI: 8. Embedding vectors
        AI->>Q: 9. Delete and recreate collection
        AI->>Q: 10. Store vectors and chunk payloads
    end
    AI->>AI: 11. Mark knowledge_ready in process memory
```

The collection is `travel-knowledge-v1`. This work is separate from embedding
each incoming question. The rebuild is not an atomic collection swap.
Without an API key, AI remains degraded while the marketplace can run.
Ingestion failures are logged and retried; service health alone does not
mean AI readiness. `GET /api/ai/ready` reports AI readiness.

## 5. Concierge quote and explicit customer confirmation

```mermaid
sequenceDiagram
    participant C as Customer / Frontend
    participant G as API Gateway
    participant AI as AI Gateway
    participant M as Embedded MCP Server
    participant ADB as PostgreSQL - assistant
    participant B as Booking Orchestrator
    participant BDB as PostgreSQL - booking
    participant D as Selected domain services
    participant DDB as Service-owned PostgreSQL databases
    Note over AI,M: Model can request create_quote, but not start_booking
    AI->>M: 1. Execute model-requested create_quote
    M->>B: 2. POST /quotes + user JWT
    B->>D: 3. Read selected offers and availability through APIs
    D->>DDB: 4. Read authoritative catalog and inventory
    DDB-->>D: 5. Current records
    D-->>B: 6. Quote inputs
    B->>BDB: 7. Persist expiring quote and fingerprint
    B-->>M: 8. Quote ID, selection, total, currency and expiry
    M->>ADB: 9. Save pending quote and stable idempotency key
    M-->>AI: 10. Structured quote result
    Note over AI,C: Normal assistant loop finishes as in flow 3
    AI-->>G: 11. Answer + pending_quote + exact confirmation phrase
    G-->>C: 12. Display quote for review
    C->>G: 13. Explicit POST /api/ai/assistant/confirm
    G->>AI: 14. POST /assistant/confirm
    AI->>ADB: 15. Validate owner, exact phrase, state and expiry
    AI->>ADB: 16. Record explicit confirmation
    AI->>M: 17. Call start_booking outside model inference
    M->>ADB: 18. Verify authorization and mark starting
    M->>B: 19. POST /bookings + stable Idempotency-Key
    Note over B,BDB: Durable Saga executes as in flow 6
    B-->>M: 20. Booking result and status
    M->>ADB: 21. Store booking ID and result
    M-->>AI: 22. Booking result
    AI-->>G: 23. Confirmation response
    G-->>C: 24. Show booking status
```

Creating a quote does not hold inventory or charge. Repeating a confirmed
request uses the same idempotency key, rather than intentionally starting a
second booking. The final status must be inspected: a response is not proof
that a booking completed.

## 6. Durable booking Saga: success and failure branches

This flow is shared by concierge confirmation and the conventional
`POST /api/booking/bookings` route. The caller must provide an authenticated,
explicit command and an idempotency key.

```mermaid
sequenceDiagram
    participant C as Authorized caller
    participant B as Booking Orchestrator
    participant DB as PostgreSQL - booking
    participant D as Flight / Hotel / Activities
    participant DDB as Domain-owned PostgreSQL databases
    participant P as Payment Service simulator
    participant PDB as PostgreSQL - payment
    C->>B: 1. POST /bookings with quote ID and idempotency key
    B->>DB: 2. Persist booking and durable step keys
    loop Selected items, in flight / hotel / activity order
        B->>D: 3. POST /reservations with step key
        D->>DDB: 4. Atomically hold inventory and record reservation
        DDB-->>D: 5. Hold committed
        D-->>B: 6. Hold ID and price
        B->>DB: 7. Persist step result and validate quoted price
    end
    B->>P: 8. POST /payments/authorize with step key
    P->>PDB: 9. Persist idempotent authorization
    P-->>B: 10. Authorization result
    B->>DB: 11. Persist payment progress
    B->>P: 12. POST /payments/capture with step key
    P->>PDB: 13. Persist idempotent capture
    P-->>B: 14. Capture result
    B->>DB: 15. Persist payment progress
    loop Successful holds
        B->>D: 16. POST /reservations/id/confirm
        D->>DDB: 17. Persist confirmed reservation
        D-->>B: 18. Confirmation result
        B->>DB: 19. Persist step result
    end
    B->>DB: 20. Mark completed and consume quote
    B-->>C: 21. Booking result
```

The diagram shows the happy path. Each failure can stop or redirect it:

- **Definite failure:** compensate successful holds and void or refund
  payment as applicable. Failed compensation remains
  `reconciliation_required`, not a claimed clean rollback.
- **Unknown payment outcome:** persist `payment_unknown` and keep holds
  pending reconciliation; a timeout is not proof of a declined payment.
- **Interrupted execution:** `/bookings/{id}/resume` resumes durable steps.
- **Payment reconciliation:** `/bookings/{id}/reconcile` queries the Payment
  operation by its stable key and retries safely when appropriate.
- Each service writes its own database. There is no cross-database atomic
  transaction and no direct Booking write into an inventory database.

## 7. Domain events and eventual search indexing

```mermaid
sequenceDiagram
    participant D as Event-producing domain service
    participant DB as Its PostgreSQL database
    participant W as Its background outbox publisher
    participant R as Redpanda
    participant S as Search consumer and indexer
    participant SDB as PostgreSQL - search
    participant OS as OpenSearch
    D->>DB: 1. Commit business change + outbox event together
    W->>DB: 2. Read pending outbox events
    DB-->>W: 3. Versioned event envelope
    W->>R: 4. Publish to marketplace.events.v1
    R-->>W: 5. Broker acknowledgement
    W->>DB: 6. Record successful publication
    R-->>S: 7. Deliver event to Search consumer
    S->>SDB: 8. Commit inbox, version cursor, projection and index work
    S->>R: 9. Commit consumer offset after SQL commit
    S->>SDB: 10. Read queued index work
    S->>OS: 11. Index or update search documents
    OS-->>S: 12. Indexing result
    S->>SDB: 13. Mark successful index work complete
```

Delivery is at least once. Event IDs support deduplication; aggregate
versions prevent older events from overwriting newer projections. Catalog
bootstrap events also enter this pipeline. Not every event becomes a public
search document: booking and payment state events are retained in the inbox,
while supported catalog data populates the catalog index.

Failures are retried; poison or repeatedly failing events can be directed to
`marketplace.events.dlq.v1`. Indexing and the initiating request are separate
flows: a successful domain write does not imply immediate search visibility.
Redpanda is not on the synchronous RAG retrieval or booking Saga call path.

## Implementation references

- [Frontend API calls](../frontend/src/api.ts)
- [Gateway routing](../services/gateway/app.py)
- [AI endpoints, ingestion, retrieval, and assistant loop](../services/ai_gateway/app.py)
- [Responses and embeddings adapter](../services/ai_gateway/provider.py)
- [MCP tools and domain API forwarding](../services/ai_gateway/assistant_mcp.py)
- [Booking Saga](../services/booking/app.py)
- [Event publication](../packages/common/events.py)
- [Search consumer and endpoints](../services/search/app.py)
- [OpenSearch query and sort construction](../services/search/opensearch.py)
