from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from mapinator.osm import OSMDataset
    from mapinator.traffic import TrafficSnapshot


class DomainError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail

    def __reduce__(self) -> tuple[object, tuple[int, str, str]]:
        return type(self), (self.status, self.code, self.detail)


@dataclass(frozen=True)
class Identity:
    owner_id: UUID
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class Tracking:
    session_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class NavigationRecord:
    session_id: UUID
    tracking_session_id: UUID
    route_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class Observation:
    event_id: UUID
    session_id: UUID
    sequence: int
    timestamp: datetime
    latitude: float
    longitude: float
    accuracy_m: float
    heading_deg: float | None
    speed_mps: float | None


@dataclass(frozen=True)
class SearchHit:
    source_id: str
    name: str
    latitude: float
    longitude: float
    category: str | None
    address: dict[str, str]
    location_kind: str
    dataset_id: UUID
    imported_at: datetime


@dataclass(frozen=True)
class DatasetInfo:
    dataset_id: UUID
    source_url: str
    checksum: str
    imported_at: datetime
    node_count: int
    way_count: int
    place_count: int
    bounds: tuple[float, float, float, float]
    road_geojson: dict[str, object]


class Repository(Protocol):
    def health(self) -> None: ...
    def close(self) -> None: ...
    def cleanup(self, now: datetime) -> None: ...
    def create_identity(self, token_hash: str, csrf_token: str, now: datetime) -> Identity: ...
    def identity(self, token_hash: str, now: datetime) -> Identity | None: ...
    def start_tracking(self, owner_id: UUID, now: datetime) -> Tracking: ...
    def stop_tracking(self, owner_id: UUID) -> None: ...
    def save_location(self, owner_id: UUID, value: Observation, now: datetime) -> bool: ...
    def current_location(self, owner_id: UUID, now: datetime) -> Observation | None: ...
    def search(
        self,
        query: str,
        bounds: tuple[float, float, float, float] | None,
        limit: int,
        offset: int,
    ) -> list[SearchHit]: ...
    def dataset(self) -> DatasetInfo | None: ...
    def routing_dataset(self) -> tuple[UUID, "OSMDataset"]: ...
    def save_route(
        self, owner_id: UUID, dataset_id: UUID, result: dict[str, object], now: datetime
    ) -> UUID: ...
    def route(self, owner_id: UUID, route_id: UUID, now: datetime) -> dict[str, object] | None: ...
    def traffic_consent(
        self, owner_id: UUID, session_id: UUID, enabled: bool, now: datetime
    ) -> None: ...
    def traffic_snapshot(self, graph_version: str, now: datetime) -> "TrafficSnapshot": ...
    def traffic_status(self, now: datetime) -> dict[str, object]: ...
    def navigation_start(
        self,
        owner_id: UUID,
        tracking_id: UUID,
        route_id: UUID,
        traffic_consent: bool,
        now: datetime,
    ) -> NavigationRecord: ...
    def navigation(
        self,
        owner_id: UUID,
        session_id: UUID | None,
        now: datetime,
    ) -> NavigationRecord | None: ...
