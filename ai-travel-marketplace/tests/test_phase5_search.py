from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.search.app import (
    SearchBase,
    SearchIndexOutbox,
    process_event,
    sync_search_index,
)
from services.search.opensearch import (
    build_search_query,
    normalize_document,
    search_result,
)


def test_search_query_builds_ranked_filters_and_price_ordering():
    query = build_search_query(
        query="Tokyo flight",
        kind="FLIGHT",
        city_code="tyo",
        min_price=100,
        max_price=500,
        departure_after=datetime(2026, 12, 1, tzinfo=timezone.utc),
        departure_before=datetime(2026, 12, 31, tzinfo=timezone.utc),
        sort_by="price_asc",
        limit=15,
    )

    boolean_query = query["query"]["bool"]
    assert boolean_query["must"][0]["multi_match"]["query"] == "Tokyo flight"
    assert boolean_query["must"][0]["multi_match"]["fuzziness"] == "AUTO"
    filters = boolean_query["filter"]
    assert {"term": {"kind": "flight"}} in filters
    assert {
        "bool": {
            "should": [
                {"term": {"city_code": "TYO"}},
                {"term": {"origin_city_code": "TYO"}},
                {"term": {"destination_city_code": "TYO"}},
            ],
            "minimum_should_match": 1,
        }
    } in filters
    assert {"range": {"price": {"gte": 100, "lte": 500}}} in filters
    assert {
        "range": {
            "departure_at": {
                "gte": "2026-12-01T00:00:00+00:00",
                "lte": "2026-12-31T00:00:00+00:00",
            }
        }
    } in filters
    assert query["sort"][0]["price"]["order"] == "asc"
    assert query["size"] == 15


def test_search_result_reports_projection_age_and_advisory_inventory():
    now = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
    result = search_result(
        {
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    {
                        "_source": {
                            "id": "offer-1",
                            "projection_updated_at": "2026-11-05T19:58:00+00:00",
                            "inventory_is_authoritative": False,
                        }
                    }
                ],
            }
        },
        query="Tokyo",
        sort_by="relevance",
        now=now,
    )

    assert result["total"] == 1
    assert result["freshness"]["oldest_result_age_seconds"] == 120
    assert result["freshness"]["inventory_is_authoritative"] is False
    assert result["items"][0]["inventory_is_authoritative"] is False


def test_index_outbox_retries_durably_and_measures_event_freshness(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase5.db'}")
    SearchBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    event_time = datetime.now(timezone.utc)
    event = {
        "event_id": "catalog-phase5",
        "event_type": "CatalogSnapshot",
        "schema_version": 1,
        "aggregate_type": "catalog",
        "aggregate_id": "flight",
        "aggregate_version": 1,
        "occurred_at": event_time.isoformat(),
        "producer": "flight",
        "causation_id": None,
        "payload": {
            "documents": [
                {
                    "id": "offer-001",
                    "kind": "flight",
                    "name": "Tokyo-Kyoto",
                    "origin_city_code": "TYO",
                    "destination_city_code": "KYO",
                    "price": "120.50",
                }
            ]
        },
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errors": False, "items": [{"index": {"status": 201}}]}

    class FakeClient:
        def __init__(self):
            self.bulk_body = ""

        async def head(self, path):
            assert path.endswith("marketplace-catalog-v1")
            return FakeResponse()

        async def post(self, path, *, content, headers):
            assert path == "/_bulk"
            assert headers["content-type"] == "application/x-ndjson"
            self.bulk_body = content
            return FakeResponse()

    async def stop_after_empty_batch(delay):
        assert delay >= 0
        raise asyncio.CancelledError

    client = FakeClient()
    runtime = {}
    try:
        with factory() as session:
            assert process_event(session, event)
            pending = session.get(SearchIndexOutbox, "flight:offer-001")
            assert pending is not None
            assert pending.document["price"] == 120.5
            assert pending.indexed_version == 0
            indexed_at = datetime.fromisoformat(
                pending.document["projection_updated_at"]
            )
            measured_age = max(0.0, (datetime.now(timezone.utc) - indexed_at).total_seconds())
            assert measured_age < 5

        original_sleep = asyncio.sleep
        asyncio.sleep = stop_after_empty_batch
        try:
            try:
                asyncio.run(sync_search_index(factory, client, runtime))
            except asyncio.CancelledError:
                pass
        finally:
            asyncio.sleep = original_sleep

        action, document = client.bulk_body.strip().splitlines()
        assert json.loads(action)["index"]["_id"] == "flight:offer-001"
        indexed_document = json.loads(document)
        assert indexed_document["price"] == 120.5
        assert indexed_document["inventory_is_authoritative"] is False
        with factory() as session:
            pending = session.get(SearchIndexOutbox, "flight:offer-001")
            assert pending is not None
            assert pending.indexed_version == pending.aggregate_version == 1
        assert runtime["index_connected"] is True
    finally:
        engine.dispose()


def test_normalized_documents_use_single_price_filter_field():
    projected_at = datetime(2026, 11, 5, tzinfo=timezone.utc)
    normalized = normalize_document(
        {
            "id": "hotel-1",
            "kind": "hotel",
            "name": "Tokyo Hotel",
            "starting_price": "89.25",
        },
        source="hotel",
        document_id="hotel-1",
        projected_at=projected_at,
    )

    assert normalized["price"] == 89.25
    assert normalized["starting_price"] == "89.25"
    assert normalized["source"] == "hotel"
    assert normalized["inventory_is_authoritative"] is False
