import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from mapinator.domain import (
    DatasetInfo,
    DomainError,
    Identity,
    NavigationRecord,
    Observation,
    SearchHit,
    Tracking,
)
from mapinator.osm import OSMDataset, OSMNode, OSMRelation, OSMWay, way_geometry
from mapinator.traffic import TrafficSnapshot
from mapinator.traffic_store import TrafficStore, queue_location, revoke_contribution


def make_engine(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("MAPINATOR_DATABASE_URL must use postgresql+psycopg://.")
    return create_engine(
        url,
        pool_size=4,
        max_overflow=4,
        pool_timeout=3,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=5000"},
        hide_parameters=True,
    )


def migrate(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(58180001)"))
        exists: str | None = connection.execute(
            text("SELECT to_regclass('public.mapinator_schema_versions')")
        ).scalar_one()
        versions: Sequence[int] = []
        if exists:
            versions = (
                connection.execute(
                    text("SELECT version FROM mapinator_schema_versions ORDER BY version")
                )
                .scalars()
                .all()
            )
            if list(versions) not in [list(range(1, n + 1)) for n in range(1, 6)]:
                raise ValueError("Unsupported Mapinator schema version; migration required.")
        for version, filename in (
            (1, "001_foundation.sql"),
            (2, "002_routing.sql"),
            (3, "003_traffic.sql"),
            (4, "004_navigation.sql"),
            (5, "005_discovery.sql"),
        ):
            if version not in versions:
                sql = files("mapinator").joinpath("migrations", filename).read_text()
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.exec_driver_sql(statement)


class PostgresRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def close(self) -> None:
        self.engine.dispose()

    def health(self) -> None:
        with self.engine.connect() as connection:
            version: Sequence[int] = (
                connection.execute(
                    text("SELECT version FROM mapinator_schema_versions ORDER BY version")
                )
                .scalars()
                .all()
            )
            if list(version) != [1, 2, 3, 4, 5]:
                raise DomainError(503, "schema_not_ready", "Database schema is not ready.")
            connection.execute(text("SELECT PostGIS_Version()")).scalar_one()

    def cleanup(self, now: datetime) -> None:
        with self.engine.begin() as connection:
            for table in (
                "current_locations",
                "navigation_sessions",
                "tracking_sessions",
                "saved_routes",
                "browser_sessions",
                "traffic_outbox",
                "traffic_inbox",
                "traffic_offsets",
                "traffic_samples",
                "traffic_dead_letters",
            ):
                deleted = connection.execute(
                    text(f"DELETE FROM {table} WHERE expires_at <= :now"), {"now": now}
                )
                if table == "traffic_outbox" and deleted.rowcount > 0:
                    logging.getLogger("mapinator").warning(
                        json.dumps(
                            {
                                "event": "traffic_outbox_expired",
                                "discarded": deleted.rowcount,
                            }
                        )
                    )
            connection.execute(
                text("DELETE FROM traffic_history WHERE bucket < :cutoff"),
                {"cutoff": now - timedelta(days=90)},
            )
            connection.execute(
                text("DELETE FROM traffic_estimates WHERE observed_at < :cutoff"),
                {"cutoff": now - timedelta(hours=1)},
            )

    def create_identity(self, token_hash: str, csrf_token: str, now: datetime) -> Identity:
        identity = Identity(uuid4(), csrf_token, now + timedelta(hours=24))
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO browser_sessions VALUES (:hash, :owner, :csrf, :expiry)
            """),
                {
                    "hash": token_hash,
                    "owner": identity.owner_id,
                    "csrf": csrf_token,
                    "expiry": identity.expires_at,
                },
            )
        return identity

    def identity(self, token_hash: str, now: datetime) -> Identity | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT owner_id, csrf_token, expires_at FROM browser_sessions
                WHERE token_hash = :hash AND expires_at > :now
            """),
                    {"hash": token_hash, "now": now},
                )
                .mappings()
                .one_or_none()
            )
        return Identity(row["owner_id"], row["csrf_token"], row["expires_at"]) if row else None

    def start_tracking(self, owner_id: UUID, now: datetime) -> Tracking:
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT owner_id FROM browser_sessions WHERE owner_id = :owner FOR UPDATE"),
                {"owner": owner_id},
            ).scalar_one()
            revoke_contribution(connection, owner_id, now)
            connection.execute(
                text("DELETE FROM tracking_sessions WHERE owner_id = :owner"), {"owner": owner_id}
            )
            row = (
                connection.execute(
                    text("""
                INSERT INTO tracking_sessions(session_id, owner_id, expires_at)
                VALUES (:id, :owner, :expiry)
                RETURNING session_id, expires_at
            """),
                    {"id": uuid4(), "owner": owner_id, "expiry": now + timedelta(hours=24)},
                )
                .mappings()
                .one()
            )
        return Tracking(row["session_id"], row["expires_at"])

    def navigation_start(
        self,
        owner_id: UUID,
        tracking_id: UUID,
        route_id: UUID,
        traffic_consent: bool,
        now: datetime,
    ) -> NavigationRecord:
        with self.engine.begin() as connection:
            tracking_expiry = connection.execute(
                text("""
                SELECT expires_at FROM tracking_sessions
                WHERE session_id=:tracking AND owner_id=:owner AND expires_at>:now FOR UPDATE
            """),
                {"tracking": tracking_id, "owner": owner_id, "now": now},
            ).scalar_one_or_none()
            route_expiry = connection.execute(
                text("""
                SELECT expires_at FROM saved_routes
                WHERE route_id=:route AND owner_id=:owner AND expires_at>:now
            """),
                {"route": route_id, "owner": owner_id, "now": now},
            ).scalar_one_or_none()
            if tracking_expiry is None or route_expiry is None:
                raise DomainError(
                    404, "navigation_resources_unavailable", "Active owned route/tracking required."
                )
            if not traffic_consent:
                revoke_contribution(connection, owner_id, now)
            connection.execute(
                text("""
                UPDATE tracking_sessions SET traffic_consent=:consent WHERE session_id=:tracking
            """),
                {"tracking": tracking_id, "consent": traffic_consent},
            )
            connection.execute(
                text("DELETE FROM navigation_sessions WHERE owner_id=:owner"), {"owner": owner_id}
            )
            record = NavigationRecord(
                uuid4(), tracking_id, route_id, min(tracking_expiry, route_expiry)
            )
            connection.execute(
                text("""
                INSERT INTO navigation_sessions VALUES (:id,:tracking,:owner,:route,:expiry)
            """),
                {
                    "id": record.session_id,
                    "tracking": tracking_id,
                    "owner": owner_id,
                    "route": route_id,
                    "expiry": record.expires_at,
                },
            )
        return record

    def navigation(
        self,
        owner_id: UUID,
        session_id: UUID | None,
        now: datetime,
    ) -> NavigationRecord | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT n.session_id,n.tracking_session_id,n.route_id,n.expires_at
                FROM navigation_sessions n
                JOIN tracking_sessions t ON t.session_id=n.tracking_session_id
                JOIN saved_routes r ON r.route_id=n.route_id
                WHERE n.owner_id=:owner AND n.expires_at>:now AND t.expires_at>:now
                  AND r.expires_at>:now
                  AND (CAST(:id AS uuid) IS NULL OR n.session_id=CAST(:id AS uuid))
            """),
                    {"owner": owner_id, "id": str(session_id) if session_id else None, "now": now},
                )
                .mappings()
                .one_or_none()
            )
        return NavigationRecord(**row) if row else None

    def stop_tracking(self, owner_id: UUID) -> None:
        with self.engine.begin() as connection:
            revoke_contribution(connection, owner_id, datetime.now(UTC))
            connection.execute(
                text("DELETE FROM tracking_sessions WHERE owner_id = :owner"), {"owner": owner_id}
            )

    def save_location(self, owner_id: UUID, value: Observation, now: datetime) -> bool:
        with self.engine.begin() as connection:
            session = connection.execute(
                text("""
                SELECT traffic_consent FROM tracking_sessions
                WHERE session_id = :id AND owner_id = :owner AND expires_at > :now
                FOR UPDATE
            """),
                {"id": value.session_id, "owner": owner_id, "now": now},
            ).scalar_one_or_none()
            if session is None:
                raise DomainError(
                    403, "tracking_not_allowed", "Tracking is disabled or inaccessible."
                )
            previous = (
                connection.execute(
                    text("""
                SELECT * FROM current_locations WHERE session_id = :id
            """),
                    {"id": value.session_id},
                )
                .mappings()
                .one_or_none()
            )
            if previous:
                if previous["event_id"] == value.event_id:
                    if (
                        previous["sequence"] != value.sequence
                        or previous["observed_at"] != value.timestamp
                        or previous["latitude"] != value.latitude
                        or previous["longitude"] != value.longitude
                        or previous["accuracy_m"] != value.accuracy_m
                        or previous["heading_deg"] != value.heading_deg
                        or previous["speed_mps"] != value.speed_mps
                    ):
                        raise DomainError(
                            409, "event_id_reused", "Event ID was already used with different data."
                        )
                    return False
                if (
                    value.sequence <= previous["sequence"]
                    or value.timestamp <= previous["observed_at"]
                ):
                    raise DomainError(
                        409, "stale_location", "Observation is older than current state."
                    )
            connection.execute(
                text("""
                INSERT INTO current_locations
                VALUES (:session, :event, :sequence, :observed, :lat, :lon,
                        :accuracy, :heading, :speed, :expiry)
                ON CONFLICT (session_id) DO UPDATE SET
                  event_id = EXCLUDED.event_id, sequence = EXCLUDED.sequence,
                  observed_at = EXCLUDED.observed_at, latitude = EXCLUDED.latitude,
                  longitude = EXCLUDED.longitude, accuracy_m = EXCLUDED.accuracy_m,
                  heading_deg = EXCLUDED.heading_deg, speed_mps = EXCLUDED.speed_mps,
                  expires_at = EXCLUDED.expires_at
            """),
                {
                    "session": value.session_id,
                    "event": value.event_id,
                    "sequence": value.sequence,
                    "observed": value.timestamp,
                    "lat": value.latitude,
                    "lon": value.longitude,
                    "accuracy": value.accuracy_m,
                    "heading": value.heading_deg,
                    "speed": value.speed_mps,
                    "expiry": now + timedelta(minutes=5),
                },
            )
            if session:
                queue_location(connection, owner_id, value, now)
        return True

    def traffic_consent(
        self, owner_id: UUID, session_id: UUID, enabled: bool, now: datetime
    ) -> None:
        with self.engine.begin() as connection:
            found = connection.execute(
                text("""
                UPDATE tracking_sessions SET traffic_consent=:enabled
                WHERE owner_id=:owner AND session_id=:id AND expires_at>:now
                RETURNING session_id
            """),
                {"enabled": enabled, "owner": owner_id, "id": session_id, "now": now},
            ).scalar_one_or_none()
            if found is None:
                raise DomainError(
                    403, "tracking_not_allowed", "Start authorized location tracking first."
                )
            if not enabled:
                revoke_contribution(connection, owner_id, now)

    def traffic_snapshot(self, graph_version: str, now: datetime) -> TrafficSnapshot:
        return TrafficStore(self.engine).snapshot(graph_version, now)

    def traffic_status(self, now: datetime) -> dict[str, object]:
        return TrafficStore(self.engine).status(now)

    def current_location(self, owner_id: UUID, now: datetime) -> Observation | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT l.* FROM current_locations l JOIN tracking_sessions s USING(session_id)
                WHERE s.owner_id = :owner AND s.expires_at > :now AND l.expires_at > :now
            """),
                    {"owner": owner_id, "now": now},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return Observation(
            row["event_id"],
            row["session_id"],
            row["sequence"],
            row["observed_at"],
            row["latitude"],
            row["longitude"],
            row["accuracy_m"],
            row["heading_deg"],
            row["speed_mps"],
        )

    def search(
        self,
        query: str,
        bounds: tuple[float, float, float, float] | None,
        limit: int,
        offset: int,
    ) -> list[SearchHit]:
        sql = """
            SELECT p.*, ST_Y(p.location) AS lat, ST_X(p.location) AS lon, d.imported_at
            FROM places p JOIN osm_datasets d USING(dataset_id)
            WHERE d.active AND p.search_text @@ plainto_tsquery('simple', :query)
        """
        params: dict[str, object] = {"query": query, "limit": limit, "offset": offset}
        if bounds is not None:
            sql += " AND ST_Covers(ST_MakeEnvelope(:west,:south,:east,:north,4326), p.location)"
            params.update(zip(("west", "south", "east", "north"), bounds, strict=True))
        sql += """
            ORDER BY ts_rank(p.search_text, plainto_tsquery('simple', :query)) DESC, p.source_id
            LIMIT :limit OFFSET :offset
        """
        with self.engine.connect() as connection:
            if (
                connection.execute(
                    text("SELECT 1 FROM osm_datasets WHERE active")
                ).scalar_one_or_none()
                is None
            ):
                raise DomainError(
                    503, "dataset_not_imported", "Import an OSM extract before geocoding."
                )
            rows = connection.execute(text(sql), params).mappings().all()
        return [
            SearchHit(
                r["source_id"],
                r["name"],
                r["lat"],
                r["lon"],
                r["category"],
                r["address"],
                r["location_kind"],
                r["dataset_id"],
                r["imported_at"],
            )
            for r in rows
        ]

    def dataset(self) -> DatasetInfo | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT *, ST_XMin(bounds) AS west, ST_YMin(bounds) AS south,
                          ST_XMax(bounds) AS east, ST_YMax(bounds) AS north
                FROM osm_datasets WHERE active
            """)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            features = (
                connection.execute(
                    text("""
                SELECT osm_id, tags->>'name' AS name, ST_AsGeoJSON(geometry)::json AS geometry
                FROM osm_ways WHERE dataset_id = :id AND tags ? 'highway'
                ORDER BY osm_id LIMIT 10001
            """),
                    {"id": row["dataset_id"]},
                )
                .mappings()
                .all()
            )
            if len(features) > 10000:
                raise DomainError(422, "map_too_large", "Map geometry exceeds the phase 1 limit.")
        geojson: dict[str, object] = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": f["geometry"],
                    "properties": {"osm_id": f["osm_id"], "name": f["name"]},
                }
                for f in features
            ],
        }
        return DatasetInfo(
            row["dataset_id"],
            row["source_url"],
            row["checksum"],
            row["imported_at"],
            row["node_count"],
            row["way_count"],
            row["place_count"],
            (row["west"], row["south"], row["east"], row["north"]),
            geojson,
        )

    def routing_dataset(self) -> tuple[UUID, OSMDataset]:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT dataset_id, checksum, routing_ready FROM osm_datasets WHERE active
            """)
                )
                .mappings()
                .one_or_none()
            )
            if row is None or not row["routing_ready"]:
                raise DomainError(
                    503, "graph_not_ready", "Reimport OSM XML with phase 2 to include restrictions."
                )
            identifier: UUID = row["dataset_id"]
            nodes = (
                connection.execute(
                    text("""
                SELECT osm_id, ST_Y(location) AS lat, ST_X(location) AS lon, tags
                FROM osm_nodes WHERE dataset_id=:id
            """),
                    {"id": identifier},
                )
                .mappings()
                .all()
            )
            ways = (
                connection.execute(
                    text("""
                SELECT osm_id, node_ids, tags FROM osm_ways WHERE dataset_id=:id
            """),
                    {"id": identifier},
                )
                .mappings()
                .all()
            )
            relations = (
                connection.execute(
                    text("""
                SELECT osm_id, members, tags FROM osm_relations WHERE dataset_id=:id
            """),
                    {"id": identifier},
                )
                .mappings()
                .all()
            )
        return identifier, OSMDataset(
            row["checksum"],
            tuple(OSMNode(n["osm_id"], n["lat"], n["lon"], n["tags"]) for n in nodes),
            tuple(OSMWay(w["osm_id"], tuple(w["node_ids"]), w["tags"]) for w in ways),
            (),
            (0, 0, 0, 0),
            tuple(
                OSMRelation(r["osm_id"], tuple((m[0], m[1], m[2]) for m in r["members"]), r["tags"])
                for r in relations
            ),
        )

    def save_route(
        self, owner_id: UUID, dataset_id: UUID, result: dict[str, object], now: datetime
    ) -> UUID:
        identifier = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO saved_routes VALUES (:id,:owner,:dataset,CAST(:result AS jsonb),:expiry)
            """),
                {
                    "id": identifier,
                    "owner": owner_id,
                    "dataset": dataset_id,
                    "result": json.dumps(
                        {**result, "route_id": str(identifier), "dataset_id": str(dataset_id)}
                    ),
                    "expiry": now + timedelta(hours=24),
                },
            )
        return identifier

    def route(self, owner_id: UUID, route_id: UUID, now: datetime) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            result: dict[str, object] | None = connection.execute(
                text("""
                SELECT result FROM saved_routes
                WHERE route_id=:id AND owner_id=:owner AND expires_at > :now
            """),
                {"id": route_id, "owner": owner_id, "now": now},
            ).scalar_one_or_none()
        return result


def import_dataset(engine: Engine, dataset: OSMDataset, source_url: str) -> UUID:
    identifier = uuid4()
    now = datetime.now(UTC)
    nodes = {node.osm_id: node for node in dataset.nodes}
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '120s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(58180002)"))
        existing = connection.execute(
            text("""
            SELECT dataset_id FROM osm_datasets WHERE checksum = :checksum
        """),
            {"checksum": dataset.checksum},
        ).scalar_one_or_none()
        if existing is not None:
            identifier = existing
        else:
            connection.execute(
                text("""
                INSERT INTO osm_datasets(dataset_id,checksum,source_url,imported_at,node_count,
                                         way_count,place_count,bounds,active)
                VALUES (:id,:checksum,:source,:now,:nodes,:ways,:places,
                        ST_MakeEnvelope(:west,:south,:east,:north,4326),false)
            """),
                {
                    "id": identifier,
                    "checksum": dataset.checksum,
                    "source": source_url,
                    "now": now,
                    "nodes": len(dataset.nodes),
                    "ways": len(dataset.ways),
                    "places": len(dataset.places),
                    **dict(zip(("west", "south", "east", "north"), dataset.bounds, strict=True)),
                },
            )
            connection.execute(
                text("""
                INSERT INTO osm_nodes VALUES
                (:dataset,:id,ST_SetSRID(ST_MakePoint(:lon,:lat),4326),CAST(:tags AS jsonb))
            """),
                [
                    {
                        "dataset": identifier,
                        "id": n.osm_id,
                        "lat": n.latitude,
                        "lon": n.longitude,
                        "tags": json.dumps(n.tags),
                    }
                    for n in dataset.nodes
                ],
            )
            if dataset.ways:
                connection.execute(
                    text("""
                    INSERT INTO osm_ways VALUES
                    (:dataset,:id,:refs,ST_SetSRID(ST_GeomFromGeoJSON(:geometry),4326),
                     CAST(:tags AS jsonb))
                """),
                    [
                        {
                            "dataset": identifier,
                            "id": w.osm_id,
                            "refs": list(w.refs),
                            "geometry": way_geometry(w, nodes),
                            "tags": json.dumps(w.tags),
                        }
                        for w in dataset.ways
                    ],
                )
            if dataset.places:
                connection.execute(
                    text("""
                    INSERT INTO places(dataset_id,source_id,name,category,address,tags,
                                       location_kind,location)
                    VALUES (:dataset,:source,:name,:category,CAST(:address AS jsonb),
                            CAST(:tags AS jsonb),:kind,
                            ST_PointOnSurface(ST_SetSRID(ST_GeomFromGeoJSON(:geometry),4326)))
                """),
                    [
                        {
                            "dataset": identifier,
                            "source": p.source_id,
                            "name": p.name,
                            "category": p.category,
                            "address": json.dumps(p.address),
                            "tags": json.dumps(p.tags),
                            "kind": p.location_kind,
                            "geometry": p.geometry,
                        }
                        for p in dataset.places
                    ],
                )
        connection.execute(
            text("DELETE FROM osm_relations WHERE dataset_id=:id"), {"id": identifier}
        )
        if dataset.relations:
            connection.execute(
                text("""
                INSERT INTO osm_relations
                VALUES (:dataset,:id,CAST(:members AS jsonb),CAST(:tags AS jsonb))
            """),
                [
                    {
                        "dataset": identifier,
                        "id": r.osm_id,
                        "members": json.dumps(r.members),
                        "tags": json.dumps(r.tags),
                    }
                    for r in dataset.relations
                ],
            )
        connection.execute(
            text("UPDATE osm_datasets SET routing_ready=true WHERE dataset_id=:id"),
            {"id": identifier},
        )
        connection.execute(text("UPDATE osm_datasets SET active = false WHERE active"))
        connection.execute(
            text("UPDATE osm_datasets SET active = true WHERE dataset_id = :id"), {"id": identifier}
        )
    return identifier
