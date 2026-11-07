# Phase 5: OpenSearch quality and freshness

**Status: Implemented and covered by local contract/unit tests. Docker,
PostgreSQL, Redpanda, and OpenSearch runtime behavior remains unverified in
this environment.**

## Architecture and recovery

- OpenSearch is the query/indexing engine; the Search service's SQL database
  remains the durable inbox and projection source.
- A SQL-backed search-index outbox is written in the same transaction as each
  catalog projection. A background worker sends bounded bulk requests and only
  marks a document indexed after OpenSearch confirms each item.
- Failed index operations remain pending and retry. The status endpoint
  reports pending document count and OpenSearch connectivity.
- The index is `marketplace-catalog-v1`. Its explicit mapping covers common
  search fields; dynamic fields allow additive catalog attributes.
- Rebuild clears and regenerates the SQL projection and index queue from the
  durable inbox. A newly created OpenSearch index automatically requeues saved
  projections.

## Search API

`GET /api/search/search` accepts:

- `query`: fuzzy multi-field text search; name has the strongest relevance
  boost, followed by description and route/city codes.
- `kind`: exact document type, such as `flight`, `hotel`, or `activity`.
- `city_code`: exact match against city, origin, or destination.
- `min_price` / `max_price`: inclusive range against normalized price. Flight
  fares and hotel/activity starting prices are normalized to `price`.
- `departure_after` / `departure_before`: inclusive flight departure range.
- `sort_by`: `relevance` (default), `price_asc`, `price_desc`, `rating_desc`,
  or `departure_asc`.
- `limit`: 1–100.

Example:

```text
/api/search/search?query=Tokyo&kind=flight&city_code=TYO&min_price=50&max_price=500&sort_by=price_asc
```

The response contains `items`, `total`, query/sort metadata, and a `freshness`
object with the age of the oldest returned projection. Each indexed document
also includes its projection timestamp and `inventory_is_authoritative: false`.
Search availability and prices are snapshots only; reservation/booking
services must revalidate current price and capacity.

## Declared freshness test

`tests/test_phase5_search.py` runs locally without Docker. It creates a catalog
event with a current timestamp, projects it into SQLite, measures the age of
the durable indexing payload, and sends that payload through a fake
OpenSearch-compatible bulk response. The test asserts the measured queue
payload age is below five seconds and that the row is acknowledged only after
the successful bulk response. This is a deterministic contract test, not a
broker-to-search latency benchmark.

Compose adds a single-node OpenSearch 2.19.1 service with a 512 MiB heap and a
persistent local data volume. Security is disabled only for this development
network; do not expose this configuration as a production service.
