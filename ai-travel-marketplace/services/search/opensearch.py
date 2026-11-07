from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


INDEX_NAME = "marketplace-catalog-v1"


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def index_mapping() -> dict[str, Any]:
    return {
        "mappings": {
            "dynamic": True,
            "properties": {
                "id": {"type": "keyword"},
                "source": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "name": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                },
                "description": {"type": "text"},
                "city_code": {"type": "keyword"},
                "origin_city_code": {"type": "keyword"},
                "destination_city_code": {"type": "keyword"},
                "price": {"type": "double"},
                "rating": {"type": "double"},
                "departure_at": {"type": "date"},
                "arrival_at": {"type": "date"},
                "projection_updated_at": {"type": "date"},
                "inventory_is_authoritative": {"type": "boolean"},
            },
        }
    }


def normalize_document(
    document: dict[str, Any],
    *,
    source: str,
    document_id: str,
    projected_at: datetime,
) -> dict[str, Any]:
    normalized = dict(document)
    normalized["source"] = source
    normalized["document_id"] = document_id
    price = normalized.get("price", normalized.get("starting_price"))
    if price is not None:
        normalized["price"] = float(price)
    normalized["projection_updated_at"] = as_utc(projected_at).isoformat()
    normalized["inventory_is_authoritative"] = False
    return normalized


def build_search_query(
    *,
    query: str | None,
    kind: str | None,
    city_code: str | None,
    min_price: float | None,
    max_price: float | None,
    departure_after: datetime | None,
    departure_before: datetime | None,
    sort_by: str,
    limit: int,
) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    if kind:
        filters.append({"term": {"kind": kind.lower()}})
    if city_code:
        normalized_city = city_code.upper()
        filters.append(
            {
                "bool": {
                    "should": [
                        {"term": {"city_code": normalized_city}},
                        {"term": {"origin_city_code": normalized_city}},
                        {"term": {"destination_city_code": normalized_city}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    if min_price is not None or max_price is not None:
        price_range: dict[str, float] = {}
        if min_price is not None:
            price_range["gte"] = min_price
        if max_price is not None:
            price_range["lte"] = max_price
        filters.append({"range": {"price": price_range}})
    if departure_after is not None or departure_before is not None:
        departure_range: dict[str, str] = {}
        if departure_after is not None:
            departure_range["gte"] = as_utc(departure_after).isoformat()
        if departure_before is not None:
            departure_range["lte"] = as_utc(departure_before).isoformat()
        filters.append({"range": {"departure_at": departure_range}})

    must: list[dict[str, Any]] = []
    if query and query.strip():
        must.append(
            {
                "multi_match": {
                    "query": query.strip(),
                    "fields": [
                        "name^4",
                        "description^2",
                        "city_code^2",
                        "origin_city_code^2",
                        "destination_city_code^2",
                    ],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                    "operator": "and",
                }
            }
        )

    sort_options: dict[str, list[dict[str, Any]]] = {
        "relevance": [{"_score": "desc"}, {"document_id": "asc"}],
        "price_asc": [
            {"price": {"order": "asc", "missing": "_last"}},
            {"document_id": "asc"},
        ],
        "price_desc": [
            {"price": {"order": "desc", "missing": "_last"}},
            {"document_id": "asc"},
        ],
        "rating_desc": [
            {"rating": {"order": "desc", "missing": "_last"}},
            {"document_id": "asc"},
        ],
        "departure_asc": [
            {"departure_at": {"order": "asc", "missing": "_last"}},
            {"document_id": "asc"},
        ],
    }
    return {
        "size": limit,
        "track_total_hits": True,
        "query": {"bool": {"must": must, "filter": filters}},
        "sort": sort_options[sort_by],
    }


def search_result(
    response: dict[str, Any],
    *,
    query: str | None,
    sort_by: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    hits = response.get("hits", {})
    documents: list[dict[str, Any]] = []
    ages: list[float] = []
    for hit in hits.get("hits", []):
        source = hit.get("_source", {})
        projected_at = source.get("projection_updated_at")
        if isinstance(projected_at, str):
            timestamp = datetime.fromisoformat(projected_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            ages.append(max(0.0, (current_time - timestamp).total_seconds()))
        documents.append(source)

    total = hits.get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    return {
        "items": documents,
        "total": total,
        "query": query,
        "sort_by": sort_by,
        "freshness": {
            "oldest_result_age_seconds": max(ages) if ages else None,
            "inventory_is_authoritative": False,
            "message": "Search inventory is advisory; confirm availability with the booking service.",
        },
    }
