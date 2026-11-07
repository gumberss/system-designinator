# Phase 6: AI Gateway and retrieval-augmented answers

**Status: Implemented and covered by automated contract tests. Live OpenAI,
Qdrant, and Docker Compose behavior has not been exercised in this environment.**

## Scope

Phase 6 supplies an authenticated, read-only AI foundation. It does not add
conversation persistence, model tool calling, MCP, itinerary writes, booking,
or payment actions; those remain Phase 7. The AI answer is advisory, and
transactional search/booking services remain authoritative.

## Provider and model configuration

The AI Gateway isolates provider HTTP details behind an internal chat and
embedding adapter. The local OpenAI-compatible defaults are:

- Chat: `gpt-4.1-mini`
- Embeddings: `text-embedding-3-small`
- Vector dimensions: `1536`

Configure `OPENAI_API_KEY` in the ignored local `.env` file. Model names,
embedding dimensions, and the OpenAI-compatible API base URL are configurable
with `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`,
`OPENAI_EMBEDDING_DIMENSIONS`, and `OPENAI_BASE_URL`. No key is required to
run the other marketplace services. Without a key, AI requests fail clearly
with HTTP 503; search, booking, and service health remain available.

## Curated RAG

- Original curated English Markdown sources live under `knowledge/`; they are
  the source of truth for the Phase 6 corpus.
- Ingestion splits documents into bounded, deterministic chunks and stores
  source ID/URI, title, document revision, corpus revision, language, and chunk
  number alongside each vector.
- The Qdrant collection is `travel-knowledge-v1`. A corpus revision/count
  check avoids re-embedding an already indexed corpus. Changed content is
  embedded and then replaces the derived collection; the source files remain
  available for rebuild.
- Questions are embedded, and at most five nearest passages are retrieved.
  Retrieved text is explicitly treated as untrusted reference data, not as
  instructions or authorization.
- The answer includes source citations with IDs, titles, and revisions. The
  prompt forbids claims about live prices, schedules, availability, and
  booking results.

## API

`POST /api/ai/chat` requires the existing identity bearer token:

```json
{
  "question": "What should I consider when planning a visit to Kyoto?"
}
```

The response contains `answer`, `citations`, `retrieved_chunks`, and `model`.
`GET /api/ai/ready` reports whether the model key and knowledge index are
ready. If OpenAI or Qdrant is unavailable, the request returns a generic 503
that does not expose credentials or internal network details.

No chat history is persisted, and no booking/payment tools are available to
this endpoint. The Gateway forwards the caller's bearer token to the AI
service, where its JWT is validated using the shared identity configuration.

## Compose and verification

The local Compose stack includes Qdrant with a named data volume and the AI
Gateway. Set the key in `.env`, then run:

```powershell
docker compose up --build -d
Invoke-RestMethod http://127.0.0.1:58080/api/ai/ready
```

`tests/test_phase6_ai.py` uses HTTP mock transports and deterministic
responses to verify model configuration, embedding order/dimensions,
chunk/revision metadata, retrieval grounding and citations, authentication,
and graceful provider/vector-store errors. These checks do not prove live
provider behavior, retrieval relevance on a production corpus, or end-to-end
latency. A real user-supplied key and a running Compose stack are still needed
for those checks.

The Compose Qdrant HTTP API is exposed only for local development and does not
enable authentication. Do not expose this development configuration beyond a
trusted machine/network.
