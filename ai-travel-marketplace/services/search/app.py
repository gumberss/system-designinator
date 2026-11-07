from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import OffsetAndMetadata, TopicPartition
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from packages.common.booking import utc_now
from packages.common.database import build_engine, session_dependency
from packages.common.events import (
    EVENTS_DLQ_TOPIC,
    EVENTS_TOPIC,
    EventEnvelope,
)
from services.search.opensearch import (
    INDEX_NAME,
    as_utc,
    build_search_query,
    index_mapping,
    normalize_document,
    search_result,
)

logger = logging.getLogger(__name__)
SEARCH_GROUP_ID = "marketplace-search-projection-v1"
MAX_CONSUMER_ATTEMPTS = 5
INDEX_BATCH_SIZE = 100


class SearchBase(DeclarativeBase):
    pass


class InboxEvent(SearchBase):
    __tablename__ = "event_inbox"
    __table_args__ = (
        UniqueConstraint("consumer_name", "event_id", name="uq_search_inbox_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    consumer_name: Mapped[str] = mapped_column(String(80))
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    event: Mapped[dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime)


class AggregateCursor(SearchBase):
    __tablename__ = "projection_aggregate_cursors"

    id: Mapped[str] = mapped_column(String(300), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)


class SearchDocument(SearchBase):
    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint("source", "document_id", name="uq_search_source_document"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(80), index=True)
    document_id: Mapped[str] = mapped_column(String(180))
    document_type: Mapped[str] = mapped_column(String(40), index=True)
    aggregate_version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SearchIndexOutbox(SearchBase):
    __tablename__ = "search_index_outbox"

    id: Mapped[str] = mapped_column(String(300), primary_key=True)
    source: Mapped[str] = mapped_column(String(80))
    document_id: Mapped[str] = mapped_column(String(180))
    aggregate_version: Mapped[int] = mapped_column(Integer)
    indexed_version: Mapped[int] = mapped_column(Integer, default=0)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ReservationProjection(SearchBase):
    __tablename__ = "search_reservation_projection"

    reservation_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    producer: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(24))
    aggregate_version: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime)


class ConsumerFailure(SearchBase):
    __tablename__ = "event_consumer_failures"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(String(1000))
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


def _cursor_id(event: EventEnvelope) -> str:
    return f"{event.producer}:{event.aggregate_type}:{event.aggregate_id}"


def _queue_search_document(
    session: Session,
    *,
    source: str,
    document_id: str,
    version: int,
    document: dict[str, Any],
    projected_at: datetime,
) -> None:
    outbox_id = f"{source}:{document_id}"
    pending = session.get(SearchIndexOutbox, outbox_id)
    if pending is None:
        session.add(
            SearchIndexOutbox(
                id=outbox_id,
                source=source,
                document_id=document_id,
                aggregate_version=version,
                indexed_version=0,
                document=normalize_document(
                    document,
                    source=source,
                    document_id=document_id,
                    projected_at=projected_at,
                ),
                updated_at=projected_at,
            )
        )
    elif version > pending.aggregate_version:
        pending.aggregate_version = version
        pending.document = normalize_document(
            document,
            source=source,
            document_id=document_id,
            projected_at=projected_at,
        )
        pending.updated_at = projected_at


def _project_event(session: Session, event: EventEnvelope) -> None:
    if event.event_type == "CatalogSnapshot":
        documents = event.payload.get("documents")
        if not isinstance(documents, list):
            raise ValueError("CatalogSnapshot payload requires a documents array")
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("CatalogSnapshot documents must be objects")
            source_id = document.get("id")
            document_type = document.get("kind")
            if not isinstance(source_id, str) or not isinstance(document_type, str):
                raise ValueError("Catalog document requires string id and kind")
            stored = session.scalar(
                select(SearchDocument).where(
                    SearchDocument.source == event.producer,
                    SearchDocument.document_id == source_id,
                )
            )
            if stored is None:
                session.add(
                    SearchDocument(
                        source=event.producer,
                        document_id=source_id,
                        document_type=document_type,
                        aggregate_version=event.aggregate_version,
                        document=document,
                        updated_at=event.occurred_at,
                    )
                )
                _queue_search_document(
                    session,
                    source=event.producer,
                    document_id=source_id,
                    version=event.aggregate_version,
                    document=document,
                    projected_at=event.occurred_at,
                )
            elif event.aggregate_version > stored.aggregate_version:
                stored.document_type = document_type
                stored.aggregate_version = event.aggregate_version
                stored.document = document
                stored.updated_at = event.occurred_at
                _queue_search_document(
                    session,
                    source=event.producer,
                    document_id=source_id,
                    version=event.aggregate_version,
                    document=document,
                    projected_at=event.occurred_at,
                )
    elif event.event_type == "ReservationStateChanged":
        reservation_id = event.payload.get("reservation_id")
        resource_type = event.payload.get("resource_type")
        resource_id = event.payload.get("resource_id")
        status = event.payload.get("status")
        if not all(isinstance(value, str) for value in (reservation_id, resource_type, resource_id, status)):
            raise ValueError("ReservationStateChanged payload is incomplete")
        current = session.get(ReservationProjection, reservation_id)
        if current is None:
            session.add(
                ReservationProjection(
                    reservation_id=reservation_id,
                    producer=event.producer,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    status=status,
                    aggregate_version=event.aggregate_version,
                    occurred_at=event.occurred_at,
                )
            )
        elif event.aggregate_version > current.aggregate_version:
            current.status = status
            current.aggregate_version = event.aggregate_version
            current.occurred_at = event.occurred_at


def process_event(session: Session, value: bytes | str | dict[str, Any]) -> bool:
    raw: Any = value
    if isinstance(value, bytes):
        raw = json.loads(value.decode("utf-8"))
    elif isinstance(value, str):
        raw = json.loads(value)
    event = EventEnvelope.model_validate(raw)
    if event.schema_version != 1:
        raise ValueError(f"Unsupported event schema version: {event.schema_version}")
    existing = session.scalar(
        select(InboxEvent).where(
            InboxEvent.consumer_name == SEARCH_GROUP_ID,
            InboxEvent.event_id == event.event_id,
        )
    )
    if existing is not None:
        return False
    cursor_id = _cursor_id(event)
    cursor = session.get(AggregateCursor, cursor_id)
    current_version = cursor.version if cursor is not None else 0
    if event.aggregate_version <= current_version:
        session.add(
            InboxEvent(
                consumer_name=SEARCH_GROUP_ID,
                event_id=event.event_id,
                event=event.model_dump(mode="json"),
                received_at=utc_now(),
            )
        )
        session.commit()
        return False
    if event.aggregate_version != current_version + 1:
        raise ValueError(
            f"Aggregate version gap for {cursor_id}: expected "
            f"{current_version + 1}, got {event.aggregate_version}"
        )
    _project_event(session, event)
    session.add(
        InboxEvent(
            consumer_name=SEARCH_GROUP_ID,
            event_id=event.event_id,
            event=event.model_dump(mode="json"),
            received_at=utc_now(),
        )
    )
    if cursor is None:
        session.add(AggregateCursor(id=cursor_id, version=event.aggregate_version))
    else:
        cursor.version = event.aggregate_version
    session.commit()
    return True


def rebuild_projection(session: Session) -> int:
    with session.begin():
        events = list(
            session.scalars(
                select(InboxEvent.event).where(
                    InboxEvent.consumer_name == SEARCH_GROUP_ID
                )
            )
        )
        session.execute(delete(SearchDocument))
        session.execute(delete(SearchIndexOutbox))
        session.execute(delete(ReservationProjection))
        ordered = sorted(
            events,
            key=lambda item: (
                item["producer"],
                item["aggregate_type"],
                item["aggregate_id"],
                item["aggregate_version"],
            ),
        )
        for data in ordered:
            _project_event(session, EventEnvelope.model_validate(data))
    return len(events)


async def ensure_search_index(client: httpx.AsyncClient) -> bool:
    response = await client.head(f"/{INDEX_NAME}")
    if response.status_code == httpx.codes.NOT_FOUND:
        created = await client.put(f"/{INDEX_NAME}", json=index_mapping())
        created.raise_for_status()
        return True
    response.raise_for_status()
    return False


async def sync_search_index(
    factory: sessionmaker[Session],
    client: httpx.AsyncClient,
    runtime: dict[str, Any],
) -> None:
    while True:
        with factory() as session:
            batch = list(
                session.scalars(
                    select(SearchIndexOutbox)
                    .where(
                        SearchIndexOutbox.indexed_version
                        < SearchIndexOutbox.aggregate_version
                    )
                    .order_by(SearchIndexOutbox.updated_at)
                    .limit(INDEX_BATCH_SIZE)
                )
            )
            documents = [
                {
                    "id": item.id,
                    "version": item.aggregate_version,
                    "document": item.document,
                }
                for item in batch
            ]
        if not documents:
            try:
                health = await client.head(f"/{INDEX_NAME}")
                health.raise_for_status()
                runtime["index_connected"] = True
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime["index_connected"] = False
                logger.exception("Unable to reach the OpenSearch index")
            await asyncio.sleep(0.5 if runtime["index_connected"] else 2.0)
            continue

        bulk_lines: list[str] = []
        for item in documents:
            bulk_lines.append(
                json.dumps(
                    {"index": {"_index": INDEX_NAME, "_id": item["id"]}},
                    separators=(",", ":"),
                )
            )
            bulk_lines.append(json.dumps(item["document"], separators=(",", ":")))
        try:
            response = await client.post(
                "/_bulk",
                content="\n".join(bulk_lines) + "\n",
                headers={"content-type": "application/x-ndjson"},
            )
            response.raise_for_status()
            result = response.json()
            items = result.get("items")
            if result.get("errors") or not isinstance(items, list) or len(items) != len(documents):
                raise RuntimeError("OpenSearch bulk indexing returned item errors")
            for item, result_item in zip(documents, items, strict=True):
                operation = result_item.get("index", {})
                if not 200 <= operation.get("status", 500) < 300:
                    raise RuntimeError(
                        f"OpenSearch failed to index {item['id']}: "
                        f"{operation.get('error', operation.get('status'))}"
                    )
            with factory() as session:
                for item in documents:
                    pending = session.get(SearchIndexOutbox, item["id"])
                    if pending is not None and pending.aggregate_version == item["version"]:
                        pending.indexed_version = item["version"]
                session.commit()
            runtime["index_connected"] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            runtime["index_connected"] = False
            logger.exception("Unable to synchronize Search projection with OpenSearch")
            await asyncio.sleep(2.0)


async def _record_consumer_failure(
    factory: sessionmaker[Session], event_id: str, error: Exception
) -> int:
    with factory() as session:
        failure = session.get(ConsumerFailure, event_id)
        if failure is None:
            failure = ConsumerFailure(
                event_id=event_id,
                attempts=0,
                last_error="",
                updated_at=utc_now(),
            )
            session.add(failure)
        failure.attempts += 1
        failure.last_error = str(error)[:1000]
        failure.updated_at = utc_now()
        session.commit()
        return failure.attempts


async def _consume_message(
    factory: sessionmaker[Session],
    producer: AIOKafkaProducer,
    consumer: AIOKafkaConsumer,
    message: Any,
    runtime: dict[str, Any],
    dlq_topic: str,
) -> None:
    event_id = ""
    try:
        raw_event = json.loads(message.value.decode("utf-8"))
        if isinstance(raw_event, dict) and isinstance(raw_event.get("event_id"), str):
            event_id = raw_event["event_id"]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if not event_id:
        event_id = hashlib.sha256(message.value).hexdigest()

    for attempt in range(MAX_CONSUMER_ATTEMPTS):
        try:
            try:
                with factory() as session:
                    process_event(session, message.value)
            except IntegrityError:
                with factory() as session:
                    already_processed = session.scalar(
                        select(InboxEvent.id).where(
                            InboxEvent.consumer_name == SEARCH_GROUP_ID,
                            InboxEvent.event_id == event_id,
                        )
                    )
                if already_processed is None:
                    raise
            partition = TopicPartition(message.topic, message.partition)
            await consumer.commit(
                {partition: OffsetAndMetadata(message.offset + 1, "")}
            )
            runtime["last_event_received_at"] = utc_now()
            try:
                highwater = await consumer.end_offsets([partition])
                position = await consumer.position(partition)
                runtime["consumer_lag"] = max(
                    0, highwater[partition] - position
                )
            except Exception:
                logger.exception("Unable to read event consumer lag")
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Search projection failed for event %s", event_id)
            attempts = await _record_consumer_failure(factory, event_id, error)
            if attempts >= MAX_CONSUMER_ATTEMPTS:
                await producer.send_and_wait(
                    dlq_topic,
                    value=json.dumps(
                        {
                            "raw_event": message.value.decode("utf-8", errors="replace"),
                            "failure": str(error),
                            "attempts": attempts,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    key=message.key,
                )
                with factory() as session:
                    failure = session.get(ConsumerFailure, event_id)
                    if failure is not None:
                        failure.dead_lettered_at = utc_now()
                        session.commit()
                partition = TopicPartition(message.topic, message.partition)
                await consumer.commit(
                    {partition: OffsetAndMetadata(message.offset + 1, "")}
                )
                return
            await asyncio.sleep(min(0.25 * (2**attempt), 5.0))


async def consume_events(
    factory: sessionmaker[Session], brokers: str, runtime: dict[str, Any]
) -> None:
    topic = os.getenv("EVENTS_TOPIC", EVENTS_TOPIC)
    dlq_topic = os.getenv("EVENTS_DLQ_TOPIC", EVENTS_DLQ_TOPIC)
    while True:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=brokers.split(","),
            group_id=SEARCH_GROUP_ID,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )
        producer = AIOKafkaProducer(
            bootstrap_servers=brokers.split(","),
            client_id="search-projection-dlq",
            enable_idempotence=True,
        )
        try:
            await producer.start()
            await consumer.start()
            runtime["consumer_connected"] = True
            async for message in consumer:
                await _consume_message(
                    factory, producer, consumer, message, runtime, dlq_topic
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Search Kafka consumer failed; reconnecting")
            runtime["consumer_connected"] = False
            await asyncio.sleep(2.0)
        finally:
            try:
                await consumer.stop()
                await producer.stop()
            except Exception:
                logger.exception("Failed to stop Kafka client cleanly")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    database_url = os.getenv("DATABASE_URL", "sqlite:///./search.db")
    engine: Engine = build_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.engine = engine
    app.state.session_factory = factory
    SearchBase.metadata.create_all(engine)
    brokers = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    runtime: dict[str, Any] = {
        "consumer_connected": False,
        "consumer_lag": None,
        "last_event_received_at": None,
        "index_connected": False,
    }
    app.state.projection_runtime = runtime
    opensearch_url = os.getenv("OPENSEARCH_URL")
    index_client = (
        httpx.AsyncClient(base_url=opensearch_url, timeout=httpx.Timeout(5.0))
        if opensearch_url
        else None
    )
    index_task = None
    if index_client is not None:
        app.state.opensearch_client = index_client
        try:
            index_created = await ensure_search_index(index_client)
            if index_created:
                with factory() as session:
                    for pending in session.scalars(select(SearchIndexOutbox)):
                        pending.indexed_version = 0
                    session.commit()
            index_task = asyncio.create_task(
                sync_search_index(factory, index_client, runtime)
            )
        except Exception:
            await index_client.aclose()
            engine.dispose()
            raise
    else:
        logger.warning("Search indexing is disabled: OPENSEARCH_URL is unset")
    consumer_task = (
        asyncio.create_task(consume_events(factory, brokers, runtime))
        if brokers
        else None
    )
    if not brokers:
        logger.warning("Search event consumption is disabled: KAFKA_BOOTSTRAP_SERVERS is unset")
    try:
        yield
    finally:
        if index_task is not None:
            index_task.cancel()
            try:
                await index_task
            except asyncio.CancelledError:
                pass
        if consumer_task is not None:
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        if index_client is not None:
            await index_client.aclose()
        engine.dispose()


app = FastAPI(title="Marketplace Search Projection", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "search", "status": "ok"}


@app.get("/search")
async def search(
    request: Request,
    query: str | None = Query(default=None, max_length=120),
    kind: str | None = Query(default=None, max_length=40),
    city_code: str | None = Query(default=None, min_length=3, max_length=3),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, ge=0),
    departure_after: datetime | None = None,
    departure_before: datetime | None = None,
    sort_by: str = Query(
        default="relevance",
        pattern="^(relevance|price_asc|price_desc|rating_desc|departure_asc)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
):
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(
            status_code=422, detail="min_price must not exceed max_price"
        )
    if (
        departure_after is not None
        and departure_before is not None
        and as_utc(departure_after) > as_utc(departure_before)
    ):
        raise HTTPException(
            status_code=422,
            detail="departure_after must not exceed departure_before",
        )
    client: httpx.AsyncClient | None = getattr(
        request.app.state, "opensearch_client", None
    )
    if client is None:
        raise HTTPException(status_code=503, detail="Search index is unavailable")
    try:
        response = await client.post(
            f"/{INDEX_NAME}/_search",
            json=build_search_query(
                query=query,
                kind=kind,
                city_code=city_code,
                min_price=min_price,
                max_price=max_price,
                departure_after=departure_after,
                departure_before=departure_before,
                sort_by=sort_by,
                limit=limit,
            ),
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=503, detail="Search index is unavailable") from error
    return search_result(
        response.json(),
        query=query,
        sort_by=sort_by,
    )


@app.get("/search/status")
def projection_status(
    request: Request, session: Session = Depends(_session)
) -> dict[str, Any]:
    latest = session.scalar(select(func.max(InboxEvent.received_at)))
    pending_index_documents = session.scalar(
        select(func.count(SearchIndexOutbox.id)).where(
            SearchIndexOutbox.indexed_version < SearchIndexOutbox.aggregate_version
        )
    )
    return {
        "consumer": SEARCH_GROUP_ID,
        "documents": session.scalar(select(func.count(SearchDocument.id))) or 0,
        "processed_events": session.scalar(select(func.count(InboxEvent.id))) or 0,
        "reservation_states": session.scalar(
            select(func.count(ReservationProjection.reservation_id))
        )
        or 0,
        "dead_lettered_events": session.scalar(
            select(func.count(ConsumerFailure.event_id)).where(
                ConsumerFailure.dead_lettered_at.is_not(None)
            )
        )
        or 0,
        "pending_index_documents": pending_index_documents or 0,
        "last_event_received_at": latest,
        "consumer_connected": getattr(request.app.state, "projection_runtime", {}).get(
            "consumer_connected", False
        ),
        "consumer_lag": getattr(request.app.state, "projection_runtime", {}).get(
            "consumer_lag"
        ),
        "index_connected": getattr(request.app.state, "projection_runtime", {}).get(
            "index_connected", False
        ),
    }


@app.post("/search/rebuild")
def rebuild(
    x_admin_token: str | None = Header(default=None),
    session: Session = Depends(_session),
) -> dict[str, int]:
    expected = os.getenv("SEARCH_ADMIN_TOKEN")
    if not expected or not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=403, detail="Search rebuild is not authorized")
    return {"replayed_events": rebuild_projection(session)}
