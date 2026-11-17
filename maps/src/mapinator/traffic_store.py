import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from mapinator.domain import Observation
from mapinator.routing import Graph
from mapinator.traffic import (
    TrafficEstimate,
    TrafficEvent,
    TrafficSample,
    TrafficSnapshot,
    aggregate,
    match_observation,
    utc_bucket,
)

LOCATION_TOPIC = "mapinator.location.v1"
UPDATE_TOPIC = "mapinator.traffic.v1"
DLQ_TOPIC = "mapinator.dead-letter.v1"


def contributor_id(owner_id: UUID, now: datetime) -> str:
    return hashlib.sha256(
        owner_id.bytes + now.astimezone(UTC).date().isoformat().encode()
    ).hexdigest()


def enqueue(
    connection: Connection,
    identifier: UUID,
    topic: str,
    key: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    connection.execute(
        text("""
        INSERT INTO traffic_outbox
        (event_id,topic,partition_key,payload,created_at,expires_at,next_attempt)
        VALUES(:id,:topic,:key,CAST(:payload AS jsonb),:now,:expiry,:now)
        ON CONFLICT(event_id) DO NOTHING
    """),
        {
            "id": identifier,
            "topic": topic,
            "key": key,
            "payload": json.dumps(payload),
            "now": now,
            "expiry": now + timedelta(hours=1),
        },
    )


def queue_location(
    connection: Connection, owner_id: UUID, value: Observation, now: datetime
) -> None:
    if value.speed_mps is None or value.heading_deg is None or value.speed_mps > 70:
        return
    version: str | None = connection.execute(
        text("SELECT checksum FROM osm_datasets WHERE active AND routing_ready")
    ).scalar_one_or_none()
    if version is None:
        return
    event = TrafficEvent(
        event_id=uuid5(NAMESPACE_URL, f"{value.session_id}:{value.event_id}"),
        session_id=value.session_id,
        contributor=contributor_id(owner_id, now),
        graph_version=version,
        sequence=value.sequence,
        observed_at=value.timestamp,
        latitude=value.latitude,
        longitude=value.longitude,
        speed_mps=value.speed_mps,
        heading_deg=value.heading_deg,
        accuracy_m=value.accuracy_m,
    )
    enqueue(
        connection,
        event.event_id,
        LOCATION_TOPIC,
        str(value.session_id),
        event.model_dump(mode="json"),
        now,
    )


def revoke_contribution(connection: Connection, owner_id: UUID, now: datetime) -> None:
    connection.execute(text("SELECT pg_advisory_xact_lock(58180003)"))
    connection.execute(
        text("""
        DELETE FROM traffic_outbox WHERE topic=:topic AND payload->>'session_id' IN
        (SELECT session_id::text FROM tracking_sessions WHERE owner_id=:owner)
    """),
        {"topic": LOCATION_TOPIC, "owner": owner_id},
    )
    contributors = [
        contributor_id(owner_id, now),
        contributor_id(owner_id, now - timedelta(days=1)),
    ]
    connection.execute(
        text("""
        DELETE FROM traffic_estimates WHERE (graph_version,edge_id) IN
        (SELECT graph_version,edge_id FROM traffic_samples WHERE contributor IN (:today,:yesterday))
    """),
        {"today": contributors[0], "yesterday": contributors[1]},
    )
    connection.execute(
        text("""
        DELETE FROM traffic_samples WHERE contributor IN (:today,:yesterday)
    """),
        {"today": contributors[0], "yesterday": contributors[1]},
    )


class TrafficStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def claim(self, now: datetime) -> dict[str, object] | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text("""
                SELECT event_id FROM traffic_outbox
                WHERE expires_at > :now AND next_attempt <= :now
                  AND (lease_until IS NULL OR lease_until <= :now)
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            """),
                {"now": now},
            ).scalar_one_or_none()
            if row is None:
                return None
            result = (
                connection.execute(
                    text("""
                UPDATE traffic_outbox SET lease_id=:lease,lease_until=:expiry,attempts=attempts+1
                WHERE event_id=:id RETURNING *
            """),
                    {"lease": uuid4(), "expiry": now + timedelta(seconds=30), "id": row},
                )
                .mappings()
                .one()
            )
            return dict(result)

    def delivered(self, identifier: UUID, lease: UUID) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                DELETE FROM traffic_outbox WHERE event_id=:id AND lease_id=:lease
            """),
                {"id": identifier, "lease": lease},
            )

    def failed(self, identifier: UUID, lease: UUID, now: datetime, code: str) -> None:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT attempts,topic FROM traffic_outbox
                WHERE event_id=:id AND lease_id=:lease FOR UPDATE
            """),
                    {"id": identifier, "lease": lease},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return
            if row["attempts"] >= 5:
                self._dead_letter(
                    connection, identifier, code, now, publish=row["topic"] != DLQ_TOPIC
                )
                connection.execute(
                    text("DELETE FROM traffic_outbox WHERE event_id=:id"), {"id": identifier}
                )
            else:
                connection.execute(
                    text("""
                    UPDATE traffic_outbox SET lease_id=NULL,lease_until=NULL,next_attempt=:next
                    WHERE event_id=:id AND lease_id=:lease
                """),
                    {
                        "id": identifier,
                        "lease": lease,
                        "next": now + timedelta(seconds=2 ** row["attempts"]),
                    },
                )

    def _dead_letter(
        self,
        connection: Connection,
        identifier: UUID,
        code: str,
        now: datetime,
        publish: bool = True,
    ) -> None:
        inserted = connection.execute(
            text("""
            INSERT INTO traffic_dead_letters VALUES(:id,:code,:now,:expiry)
            ON CONFLICT(event_id) DO NOTHING RETURNING event_id
        """),
            {"id": identifier, "code": code, "now": now, "expiry": now + timedelta(hours=1)},
        ).scalar_one_or_none()
        if inserted is not None and publish:
            enqueue(
                connection,
                uuid4(),
                DLQ_TOPIC,
                str(identifier),
                {"event_id": str(identifier), "code": code},
                now,
            )

    def reject(self, identifier: UUID, code: str, now: datetime) -> None:
        with self.engine.begin() as connection:
            self._dead_letter(connection, identifier, code, now)

    def consume(self, event: TrafficEvent, graph: Graph, graph_version: str, now: datetime) -> str:
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text("""
                INSERT INTO traffic_inbox VALUES(:id,:now,:expiry)
                ON CONFLICT(event_id) DO NOTHING RETURNING event_id
            """),
                {"id": event.event_id, "now": now, "expiry": now + timedelta(hours=1)},
            ).scalar_one_or_none()
            if inserted is None:
                return "duplicate"
            consent = connection.execute(
                text("""
                SELECT owner_id FROM tracking_sessions
                WHERE session_id=:id AND traffic_consent AND expires_at > :now FOR SHARE
            """),
                {"id": event.session_id, "now": now},
            ).scalar_one_or_none()
            if consent is None:
                return "consent_inactive"
            if event.contributor not in {
                contributor_id(consent, now),
                contributor_id(consent, now - timedelta(days=1)),
            }:
                self._dead_letter(connection, event.event_id, "invalid_contributor", now)
                return "invalid_contributor"
            if event.graph_version != graph_version:
                self._dead_letter(connection, event.event_id, "graph_version_changed", now)
                return "graph_version_changed"
            offset = connection.execute(
                text("""
                INSERT INTO traffic_offsets VALUES(:session,:sequence,:observed,:expiry)
                ON CONFLICT(session_id) DO UPDATE SET
                  sequence=EXCLUDED.sequence,observed_at=EXCLUDED.observed_at,
                  expires_at=EXCLUDED.expires_at
                WHERE traffic_offsets.sequence < EXCLUDED.sequence
                  AND traffic_offsets.observed_at < EXCLUDED.observed_at
                RETURNING session_id
            """),
                {
                    "session": event.session_id,
                    "sequence": event.sequence,
                    "observed": event.observed_at,
                    "expiry": now + timedelta(hours=1),
                },
            ).scalar_one_or_none()
            if offset is None:
                return "out_of_order"
            edge = match_observation(graph, event, now)
            if edge is None:
                return "quality_rejected"
            connection.execute(
                text("""
                INSERT INTO traffic_samples
                VALUES(:version,:edge,:contributor,:bucket,:observed,:speed,:expiry)
                ON CONFLICT(graph_version,edge_id,contributor,bucket) DO UPDATE SET
                  observed_at=EXCLUDED.observed_at,speed_mps=EXCLUDED.speed_mps,expires_at=EXCLUDED.expires_at
                WHERE traffic_samples.observed_at < EXCLUDED.observed_at
            """),
                {
                    "version": graph_version,
                    "edge": edge,
                    "contributor": event.contributor,
                    "bucket": utc_bucket(event.observed_at),
                    "observed": event.observed_at,
                    "speed": event.speed_mps,
                    "expiry": now + timedelta(hours=1),
                },
            )
            return "matched"

    def snapshot(self, graph_version: str, now: datetime) -> TrafficSnapshot:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text("""
                SELECT * FROM traffic_estimates WHERE graph_version=:version
            """),
                    {"version": graph_version},
                )
                .mappings()
                .all()
            )
            history = (
                connection.execute(
                    text("""
                SELECT edge_id,avg(speed_mps) AS speed,count(*) AS windows
                FROM traffic_history WHERE graph_version=:version AND bucket >= :cutoff
                  AND extract(isodow FROM bucket AT TIME ZONE 'UTC')=:weekday
                  AND extract(hour FROM bucket AT TIME ZONE 'UTC')=:hour
                GROUP BY edge_id
            """),
                    {
                        "version": graph_version,
                        "cutoff": now - timedelta(days=90),
                        "weekday": now.astimezone(UTC).isoweekday(),
                        "hour": now.astimezone(UTC).hour,
                    },
                )
                .mappings()
                .all()
            )
        estimates = {
            r["edge_id"]: TrafficEstimate(
                r["edge_id"],
                r["speed_mps"],
                r["observed_at"],
                r["confidence"],
                r["contributors"],
                r["congestion"],
                r["anomaly"],
            )
            for r in rows
        }
        for row in history:
            current = estimates.get(row["edge_id"], TrafficEstimate(row["edge_id"]))
            estimates[row["edge_id"]] = replace(
                current,
                historical_speed_mps=float(row["speed"]),
                historical_confidence=min(1.0, row["windows"] / 12),
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        **asdict(e),
                        "observed_at": e.observed_at.isoformat() if e.observed_at else None,
                    }
                    for _, e in sorted(estimates.items())
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
        return TrafficSnapshot(f"{fingerprint}:{now.isoformat()}", estimates)

    def aggregate_window(self, graph: Graph, graph_version: str, now: datetime) -> int:
        count = 0
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(58180003)"))
            snapshot = self.snapshot(graph_version, now)
            rows = (
                connection.execute(
                    text("""
                SELECT * FROM traffic_samples
                WHERE graph_version=:version AND observed_at >= :cutoff
            """),
                    {"version": graph_version, "cutoff": now - timedelta(minutes=10)},
                )
                .mappings()
                .all()
            )
            current: dict[str, list[TrafficSample]] = {}
            closed: dict[tuple[str, datetime], list[TrafficSample]] = {}
            for row in rows:
                sample = TrafficSample(row["contributor"], row["speed_mps"], row["observed_at"])
                current.setdefault(row["edge_id"], []).append(sample)
                if row["bucket"] < utc_bucket(now):
                    closed.setdefault((row["edge_id"], row["bucket"]), []).append(sample)
            for edge_id, samples in current.items():
                edge = graph.edges.get(edge_id)
                if edge is None or "car" not in edge.speeds:
                    continue
                published = connection.execute(
                    text("""
                    SELECT published_at FROM traffic_estimates
                    WHERE graph_version=:version AND edge_id=:edge
                """),
                    {"version": graph_version, "edge": edge_id},
                ).scalar_one_or_none()
                if published and (now - published).total_seconds() < 30:
                    continue
                estimate = aggregate(
                    edge_id, samples, edge.speeds["car"], now, snapshot.estimates.get(edge_id)
                )
                if estimate is None:
                    connection.execute(
                        text("""
                        DELETE FROM traffic_estimates WHERE graph_version=:version AND edge_id=:edge
                    """),
                        {"version": graph_version, "edge": edge_id},
                    )
                    continue
                previous = snapshot.estimates.get(edge_id)
                if previous and (
                    estimate.observed_at == previous.observed_at
                    and estimate.speed_mps == previous.speed_mps
                    and estimate.confidence == previous.confidence
                    and estimate.contributors == previous.contributors
                    and estimate.congestion == previous.congestion
                    and estimate.anomaly == previous.anomaly
                ):
                    continue
                connection.execute(
                    text("""
                    INSERT INTO traffic_estimates VALUES
                    (:version,:edge,:speed,:observed,:now,:confidence,:contributors,:congestion,:anomaly)
                    ON CONFLICT(graph_version,edge_id) DO UPDATE SET
                      speed_mps=EXCLUDED.speed_mps,observed_at=EXCLUDED.observed_at,
                      published_at=EXCLUDED.published_at,confidence=EXCLUDED.confidence,
                      contributors=EXCLUDED.contributors,congestion=EXCLUDED.congestion,
                      anomaly=EXCLUDED.anomaly
                """),
                    {
                        "version": graph_version,
                        "edge": edge_id,
                        "speed": estimate.speed_mps,
                        "observed": estimate.observed_at,
                        "now": now,
                        "confidence": estimate.confidence,
                        "contributors": estimate.contributors,
                        "congestion": estimate.congestion,
                        "anomaly": estimate.anomaly,
                    },
                )
                enqueue(
                    connection,
                    uuid4(),
                    UPDATE_TOPIC,
                    edge_id,
                    {
                        **asdict(estimate),
                        "observed_at": estimate.observed_at.isoformat()
                        if estimate.observed_at
                        else None,
                        "graph_version": graph_version,
                    },
                    now,
                )
                count += 1
            for (edge_id, bucket), samples in closed.items():
                edge = graph.edges.get(edge_id)
                if edge is None or "car" not in edge.speeds:
                    continue
                estimate = aggregate(
                    edge_id, samples, edge.speeds["car"], bucket + timedelta(minutes=5)
                )
                if estimate is not None:
                    connection.execute(
                        text("""
                        INSERT INTO traffic_history
                        VALUES(:version,:edge,:bucket,:speed,:contributors)
                        ON CONFLICT(graph_version,edge_id,bucket) DO NOTHING
                    """),
                        {
                            "version": graph_version,
                            "edge": edge_id,
                            "bucket": bucket,
                            "speed": estimate.speed_mps,
                            "contributors": estimate.contributors,
                        },
                    )
        return count

    def heartbeat(self, worker: str, now: datetime, lag: float) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO traffic_worker_state VALUES(:worker,:now,:lag)
                ON CONFLICT(worker) DO UPDATE SET heartbeat=EXCLUDED.heartbeat,lag_s=EXCLUDED.lag_s
            """),
                {"worker": worker, "now": now, "lag": max(0, lag)},
            )

    def status(self, now: datetime) -> dict[str, object]:
        with self.engine.connect() as connection:
            pending = (
                connection.execute(
                    text("""
                SELECT count(*) AS count,min(created_at) AS oldest FROM traffic_outbox
            """)
                )
                .mappings()
                .one()
            )
            workers = (
                connection.execute(text("SELECT * FROM traffic_worker_state")).mappings().all()
            )
            dead: int = connection.execute(
                text("SELECT count(*) FROM traffic_dead_letters")
            ).scalar_one()
        oldest = max(0, (now - pending["oldest"]).total_seconds()) if pending["oldest"] else 0
        healthy_workers = {
            w["worker"]
            for w in workers
            if (now - w["heartbeat"]).total_seconds() < 60 and w["lag_s"] <= 60
        }
        return {
            "pending_events": pending["count"],
            "oldest_pending_s": oldest,
            "dead_letters": dead,
            "workers": [
                {
                    "worker": w["worker"],
                    "age_s": max(0, (now - w["heartbeat"]).total_seconds()),
                    "lag_s": w["lag_s"],
                }
                for w in workers
            ],
            "status": "ready"
            if healthy_workers >= {"relay", "analytics"} and oldest <= 60 and dead == 0
            else "degraded",
        }
