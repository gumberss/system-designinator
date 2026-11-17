from datetime import datetime, timedelta
from uuid import UUID, uuid4

from mapinator.domain import (
    DatasetInfo,
    DomainError,
    Identity,
    NavigationRecord,
    Observation,
    SearchHit,
    Tracking,
)
from mapinator.osm import OSMDataset
from mapinator.traffic import TrafficSnapshot


class MemoryRepository:
    """Explicit test double, not a durable or production adapter."""

    def __init__(self) -> None:
        self.identities: dict[str, Identity] = {}
        self.tracking: dict[UUID, Tracking] = {}
        self.locations: dict[UUID, Observation] = {}
        self.location_expiry: dict[UUID, datetime] = {}
        self.hits: list[SearchHit] = []
        self.info: DatasetInfo | None = None
        self.road_data: OSMDataset | None = None
        self.routes: dict[UUID, tuple[UUID, dict[str, object], datetime]] = {}
        self.consent: set[UUID] = set()
        self.snapshot = TrafficSnapshot()
        self.navigations: dict[UUID, NavigationRecord] = {}

    def close(self) -> None:
        pass

    def health(self) -> None:
        pass

    def cleanup(self, now: datetime) -> None:
        for owner, expiry in list(self.location_expiry.items()):
            if expiry <= now:
                self.locations.pop(owner, None)
                self.location_expiry.pop(owner, None)
        for token, identity in list(self.identities.items()):
            if identity.expires_at <= now:
                self.stop_tracking(identity.owner_id)
                del self.identities[token]

    def create_identity(self, token_hash: str, csrf_token: str, now: datetime) -> Identity:
        identity = Identity(uuid4(), csrf_token, now + timedelta(hours=24))
        self.identities[token_hash] = identity
        return identity

    def identity(self, token_hash: str, now: datetime) -> Identity | None:
        identity = self.identities.get(token_hash)
        return identity if identity and identity.expires_at > now else None

    def start_tracking(self, owner_id: UUID, now: datetime) -> Tracking:
        self.stop_tracking(owner_id)
        self.tracking[owner_id] = Tracking(uuid4(), now + timedelta(hours=24))
        return self.tracking[owner_id]

    def stop_tracking(self, owner_id: UUID) -> None:
        self.navigations.pop(owner_id, None)
        existing = self.tracking.get(owner_id)
        if existing:
            self.consent.discard(existing.session_id)
        self.tracking.pop(owner_id, None)
        self.locations.pop(owner_id, None)
        self.location_expiry.pop(owner_id, None)

    def save_location(self, owner_id: UUID, value: Observation, now: datetime) -> bool:
        tracking = self.tracking.get(owner_id)
        if not tracking or tracking.session_id != value.session_id or tracking.expires_at <= now:
            raise DomainError(403, "tracking_not_allowed", "Tracking is disabled or inaccessible.")
        previous = self.locations.get(owner_id)
        if previous:
            if previous.event_id == value.event_id:
                if previous != value:
                    raise DomainError(409, "event_id_reused", "Event ID was reused.")
                return False
            if value.sequence <= previous.sequence or value.timestamp <= previous.timestamp:
                raise DomainError(409, "stale_location", "Observation is older than current state.")
        self.locations[owner_id] = value
        self.location_expiry[owner_id] = now + timedelta(minutes=5)
        return True

    def current_location(self, owner_id: UUID, now: datetime) -> Observation | None:
        tracking = self.tracking.get(owner_id)
        expiry = self.location_expiry.get(owner_id)
        if not tracking or tracking.expires_at <= now or expiry is None or expiry <= now:
            return None
        return self.locations.get(owner_id)

    def search(
        self,
        query: str,
        bounds: tuple[float, float, float, float] | None,
        limit: int,
        offset: int,
    ) -> list[SearchHit]:
        if self.info is None and not self.hits:
            raise DomainError(503, "dataset_not_imported", "Import an OSM extract first.")
        found = [hit for hit in self.hits if query.lower() in hit.name.lower()]
        if bounds:
            west, south, east, north = bounds
            found = [
                hit
                for hit in found
                if west <= hit.longitude <= east and south <= hit.latitude <= north
            ]
        return found[offset : offset + limit]

    def dataset(self) -> DatasetInfo | None:
        return self.info

    def routing_dataset(self) -> tuple[UUID, OSMDataset]:
        if self.road_data is None:
            raise DomainError(503, "graph_not_ready", "OSM routing dataset is absent.")
        return (self.info.dataset_id if self.info else UUID(int=1)), self.road_data

    def save_route(
        self, owner_id: UUID, dataset_id: UUID, result: dict[str, object], now: datetime
    ) -> UUID:
        identifier = uuid4()
        saved = {**result, "route_id": str(identifier), "dataset_id": str(dataset_id)}
        self.routes[identifier] = (owner_id, saved, now + timedelta(hours=24))
        return identifier

    def route(self, owner_id: UUID, route_id: UUID, now: datetime) -> dict[str, object] | None:
        value = self.routes.get(route_id)
        return value[1] if value and value[0] == owner_id and value[2] > now else None

    def traffic_consent(
        self, owner_id: UUID, session_id: UUID, enabled: bool, now: datetime
    ) -> None:
        current = self.tracking.get(owner_id)
        if not current or current.session_id != session_id or current.expires_at <= now:
            raise DomainError(403, "tracking_not_allowed", "Start location tracking first.")
        if enabled:
            self.consent.add(session_id)
        else:
            self.consent.discard(session_id)

    def traffic_snapshot(self, graph_version: str, now: datetime) -> TrafficSnapshot:
        return self.snapshot

    def traffic_status(self, now: datetime) -> dict[str, object]:
        return {"status": "degraded", "pending_events": 0, "workers": [], "dead_letters": 0}

    def navigation_start(
        self,
        owner_id: UUID,
        tracking_id: UUID,
        route_id: UUID,
        traffic_consent: bool,
        now: datetime,
    ) -> NavigationRecord:
        tracking = self.tracking.get(owner_id)
        if (
            not tracking
            or tracking.session_id != tracking_id
            or not self.route(owner_id, route_id, now)
        ):
            raise DomainError(
                404, "navigation_resources_unavailable", "Active owned resources required."
            )
        self.traffic_consent(owner_id, tracking_id, traffic_consent, now)
        record = NavigationRecord(uuid4(), tracking_id, route_id, tracking.expires_at)
        self.navigations[owner_id] = record
        return record

    def navigation(
        self,
        owner_id: UUID,
        session_id: UUID | None,
        now: datetime,
    ) -> NavigationRecord | None:
        record = self.navigations.get(owner_id)
        tracking = self.tracking.get(owner_id)
        return (
            record
            if (
                record
                and tracking
                and tracking.expires_at > now
                and record.expires_at > now
                and (session_id is None or record.session_id == session_id)
            )
            else None
        )
