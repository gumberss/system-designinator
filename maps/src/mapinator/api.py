import asyncio
import hashlib
import json
import logging
import secrets
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from importlib.resources import files
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from mapinator.discovery import LocalInterpreter
from mapinator.discovery_store import ConversationStore, PostgresConversationStore
from mapinator.domain import DomainError, Identity, Observation, Repository
from mapinator.navigation import NavigationCache, NavigationService, RedisNavigationCache
from mapinator.postgres import PostgresRepository, make_engine
from mapinator.routing import Mode, Strategy, calculate
from mapinator.traffic import TrafficSnapshot, TrafficSource, effective_speed

logger = logging.getLogger("mapinator")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAPINATOR_")
    database_url: SecretStr = SecretStr("")
    allowed_origins: str = "http://127.0.0.1:58180,http://localhost:58180"
    cookie_secure: bool = False
    map_style_url: str = ""
    kafka_bootstrap_servers: str = "127.0.0.1:59092"
    redis_url: SecretStr = SecretStr("")
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = ""

    @field_validator("map_style_url")
    @classmethod
    def valid_style(cls, value: str) -> str:
        local_path = value.startswith("/") and not value.startswith("//") and "\\" not in value
        if value and not (value.startswith("https://") or local_path):
            raise ValueError("Map style must be an HTTPS URL or same-origin absolute path.")
        return value


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, from_attributes=True)


class TrackingRequest(Schema):
    tracking_consent: Literal[True]


class TrafficConsentRequest(Schema):
    session_id: UUID
    enabled: bool
    mode: Literal["car"]


class LocationRequest(Schema):
    event_id: UUID
    session_id: UUID
    sequence: int = Field(ge=0, le=2**63 - 1)
    timestamp: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: float = Field(ge=0, le=100_000)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    speed_mps: float | None = Field(default=None, ge=0, le=400)

    @field_validator("timestamp")
    @classmethod
    def valid_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamp must include a timezone.")
        age = (datetime.now(UTC) - value).total_seconds()
        if not -15 <= age <= 60:
            raise ValueError("Timestamp must be between 60 seconds ago and 15 seconds ahead.")
        return value


class SessionResponse(Schema):
    csrf_token: str
    expires_at: datetime


class TrackingResponse(Schema):
    session_id: UUID
    expires_at: datetime


class LocationAccepted(Schema):
    accepted: bool
    deduplicated: bool


class NavigationStart(Schema):
    route_id: UUID
    tracking_session_id: UUID
    tracking_consent: Literal[True]
    traffic_consent: bool = False


class NavigationLocation(LocationRequest):
    navigation_session_id: UUID


class NavigationStop(Schema):
    session_id: UUID


class CurrentLocation(LocationRequest):
    freshness_s: float


class PlaceResponse(Schema):
    source_id: str
    name: str
    latitude: float
    longitude: float
    category: str | None
    address: dict[str, str]
    location_kind: str
    dataset_id: UUID
    imported_at: datetime


class SearchResponse(Schema):
    items: list[PlaceResponse]
    next_cursor: int | None
    coverage: Literal["active_local_osm_dataset"] = "active_local_osm_dataset"


class DatasetResponse(Schema):
    dataset_id: UUID
    source_url: str
    checksum: str
    imported_at: datetime
    node_count: int
    way_count: int
    place_count: int
    bounds: tuple[float, float, float, float]
    road_geojson: dict[str, object]
    license: str = "ODbL 1.0; © OpenStreetMap contributors"


class Coordinate(Schema):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class RoutePreferences(Schema):
    avoid_tolls: bool = False
    avoid_motorways: bool = False


class RouteRequest(Schema):
    source: Coordinate | Annotated[str, Field(min_length=2, max_length=200)]
    destination: Coordinate | Annotated[str, Field(min_length=2, max_length=200)]
    mode: Mode = "car"
    strategy: Strategy = "fastest"
    alternatives: int = Field(default=0, ge=0, le=2)
    preferences: RoutePreferences = Field(default_factory=RoutePreferences)


class RouteGeometry(Schema):
    type: Literal["LineString"]
    coordinates: list[tuple[float, float]]


class Instruction(Schema):
    action: Literal["depart", "continue", "turn_left", "turn_right", "arrive"]
    name: str
    distance_m: float
    coordinate: tuple[float, float]


class RoutePath(Schema):
    traffic_source: TrafficSource = "baseline"
    traffic_version: str = "baseline"
    segment_ids: list[str] = Field(default_factory=list)
    edge_durations_s: list[float] = Field(default_factory=list)
    geometry: RouteGeometry
    edge_ids: list[str]
    distance_m: float
    eta_s: float
    instructions: list[Instruction]
    expanded_states: int
    shortcuts_resolved: int


class RouteResponse(RoutePath):
    route_id: UUID
    dataset_id: UUID
    graph_version: str
    traffic_source: TrafficSource
    mode: Mode
    strategy: Strategy
    algorithm: Literal["hierarchical"]
    source_snap_m: float
    destination_snap_m: float
    segment_count: int
    portal_count: int
    warnings: list[str]
    alternatives: list[RoutePath]
    preferences: RoutePreferences = Field(default_factory=RoutePreferences)


class Metrics:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.counts: dict[tuple[str, int], int] = defaultdict(int)
        self.seconds: dict[str, float] = defaultdict(float)

    def observe(self, route: str, status: int, duration: float) -> None:
        with self.lock:
            self.counts[route, status] += 1
            self.seconds[route] += duration

    def render(self) -> str:
        with self.lock:
            lines = [
                "# TYPE mapinator_requests_total counter",
                *[
                    f'mapinator_requests_total{{route="{route}",status="{status}"}} {count}'
                    for (route, status), count in sorted(self.counts.items())
                ],
                "# TYPE mapinator_request_duration_seconds_sum counter",
                *[
                    f'mapinator_request_duration_seconds_sum{{route="{route}"}} {seconds}'
                    for route, seconds in sorted(self.seconds.items())
                ],
            ]
        return "\n".join(lines) + "\n"


def create_app(
    repository: Repository | None = None,
    settings: Settings | None = None,
    navigation_cache: NavigationCache | None = None,
    conversation_store: ConversationStore | None = None,
) -> FastAPI:
    config = settings or Settings()
    if repository is None:
        url = config.database_url.get_secret_value()
        if not url:
            raise ValueError("Set MAPINATOR_DATABASE_URL before starting the API.")
        repository = PostgresRepository(make_engine(url))
    repo = repository
    allowed = {origin.strip() for origin in config.allowed_origins.split(",")}
    metrics = Metrics()
    routing_pool = ProcessPoolExecutor(max_workers=2)
    routing_slots = threading.BoundedSemaphore(2)
    cache = navigation_cache
    if cache is None and config.redis_url.get_secret_value():
        cache = RedisNavigationCache(config.redis_url.get_secret_value())

    def run_job(job: Callable[[], dict[str, object]]) -> dict[str, object]:
        if not routing_slots.acquire(blocking=False):
            raise DomainError(429, "routing_busy", "Routing pool is full; retry later.")
        submitted = False
        try:
            future = routing_pool.submit(job)
            submitted = True
            future.add_done_callback(lambda _: routing_slots.release())
            try:
                return future.result(timeout=35)
            except FutureTimeoutError as exc:
                future.cancel()
                raise DomainError(503, "routing_timeout", "CPU job exceeded its deadline.") from exc
        except BrokenProcessPool as exc:
            raise DomainError(
                503, "routing_worker_failed", "Worker failed; restart required."
            ) from exc
        finally:
            if not submitted:
                routing_slots.release()

    async def execute(job: Callable[[], dict[str, object]]) -> dict[str, object]:
        return await asyncio.to_thread(run_job, job)

    async def recalculate(
        owner: UUID,
        source: tuple[float, float],
        destination: tuple[float, float],
        mode: Mode,
        strategy: Strategy,
        avoid_tolls: bool,
        avoid_motorways: bool,
    ) -> dict[str, object]:
        dataset_id, dataset = await asyncio.to_thread(repo.routing_dataset)
        now = datetime.now(UTC)
        try:
            snapshot = await asyncio.to_thread(repo.traffic_snapshot, dataset.checksum, now)
        except SQLAlchemyError as exc:
            logger.error(json.dumps({"event": "traffic_unavailable", "error": type(exc).__name__}))
            snapshot = TrafficSnapshot(unavailable=True)
        original = await execute(
            partial(
                calculate,
                dataset,
                source,
                destination,
                mode,
                strategy,
                0,
                avoid_tolls,
                avoid_motorways,
                snapshot,
                now,
            )
        )
        identifier = await asyncio.to_thread(
            repo.save_route, owner, dataset_id, original, datetime.now(UTC)
        )
        return {**original, "route_id": str(identifier), "dataset_id": str(dataset_id)}

    navigation = NavigationService(repo, cache, recalculate)
    conversations = conversation_store
    if conversations is None and isinstance(repo, PostgresRepository):
        conversations = PostgresConversationStore(repo.engine)
    interpreter = (
        LocalInterpreter(config.llm_model, config.llm_base_url) if config.llm_model else None
    )

    async def cleanup_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(repo.cleanup, datetime.now(UTC))
                if conversations is not None:
                    await asyncio.to_thread(conversations.cleanup, datetime.now(UTC))
            except SQLAlchemyError as exc:
                logger.error(json.dumps({"event": "retention_failed", "error": type(exc).__name__}))
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        cleanup = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            cleanup.cancel()
            try:
                await cleanup
            except asyncio.CancelledError:
                pass
            try:
                if cache is not None:
                    await cache.close()
            finally:
                await asyncio.to_thread(repo.close)
                await asyncio.to_thread(routing_pool.shutdown, wait=True, cancel_futures=True)

    app = FastAPI(title="Mapinator", version="0.7.0", lifespan=lifespan)

    @app.middleware("http")
    async def instrumentation(request: Request, call_next: RequestResponseEndpoint) -> Response:
        started = time.perf_counter()
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        duration = time.perf_counter() - started
        metrics.observe(route, response.status_code, duration)
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request.state.request_id,
                    "route": route,
                    "status": response.status_code,
                    "duration_s": round(duration, 6),
                }
            )
        )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    def problem(request: Request, status: int, code: str, detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            media_type="application/problem+json",
            content={
                "type": f"urn:mapinator:error:{code}",
                "title": code,
                "status": status,
                "detail": detail,
                "code": code,
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return problem(request, exc.status, exc.code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        return problem(request, 422, "invalid_request", "Request violates the documented schema.")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error(
            json.dumps(
                {
                    "event": "database_error",
                    "error": type(exc).__name__,
                    "request_id": request.state.request_id,
                }
            )
        )
        return problem(request, 503, "database_unavailable", "Required database is unavailable.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return problem(request, exc.status_code, "http_error", str(exc.detail))

    def origin_guard(request: Request) -> None:
        if request.headers.get("origin") not in allowed:
            raise DomainError(
                403, "origin_not_allowed", "Use the configured same-origin web client."
            )

    def authenticate(request: Request) -> Identity:
        token = request.cookies.get("mapinator_session")
        identity = repo.identity(_token_hash(token), datetime.now(UTC)) if token else None
        if identity is None:
            raise DomainError(401, "authentication_required", "Create a browser session first.")
        return identity

    def mutation_guard(
        request: Request, identity: Annotated[Identity, Depends(authenticate)]
    ) -> Identity:
        origin_guard(request)
        supplied = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(identity.csrf_token, supplied):
            raise DomainError(403, "csrf_failed", "Missing or invalid CSRF token.")
        return identity

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        repo.health()
        return {"status": "ready", "phase": "7"}

    @app.get("/metrics", response_class=PlainTextResponse)
    def metric_output() -> str:
        return metrics.render()

    @app.get("/api/v1/config")
    def client_config() -> dict[str, object]:
        return {
            "phase": 7,
            "map_style_url": config.map_style_url,
            "local_model_configured": bool(config.llm_model),
            "capabilities": {
                "geocoding": True,
                "location": True,
                "routing": True,
                "traffic": True,
                "navigation": True,
                "ev": True,
                "conversational_search": True,
            },
        }

    @app.post("/api/v1/auth/session", response_model=SessionResponse)
    def browser_session(request: Request, response: Response) -> SessionResponse:
        origin_guard(request)
        token = request.cookies.get("mapinator_session")
        now = datetime.now(UTC)
        existing = repo.identity(_token_hash(token), now) if token else None
        if existing:
            return SessionResponse.model_validate(existing)
        token = secrets.token_urlsafe(32)
        identity = repo.create_identity(_token_hash(token), secrets.token_urlsafe(32), now)
        response.set_cookie(
            "mapinator_session",
            token,
            httponly=True,
            samesite="strict",
            secure=config.cookie_secure,
            max_age=86_400,
        )
        response.status_code = 201
        return SessionResponse.model_validate(identity)

    @app.post("/api/v1/location/tracking", response_model=TrackingResponse, status_code=201)
    def start_tracking(
        payload: TrackingRequest, identity: Annotated[Identity, Depends(mutation_guard)]
    ) -> TrackingResponse:
        return TrackingResponse.model_validate(
            repo.start_tracking(identity.owner_id, datetime.now(UTC))
        )

    @app.delete("/api/v1/location/current")
    async def stop_tracking(
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> dict[str, bool]:
        await navigation.stop(identity.owner_id)
        return {"tracking_enabled": False, "location_deleted": True}

    @app.get("/health/navigation")
    async def navigation_health() -> dict[str, str]:
        await navigation.required_cache().health()
        return {"status": "ready"}

    @app.post("/api/v1/navigation/start", status_code=201)
    async def navigation_start(
        payload: NavigationStart,
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> dict[str, object]:
        return await navigation.start(
            identity.owner_id,
            payload.route_id,
            payload.tracking_session_id,
            payload.traffic_consent,
        )

    @app.post("/api/v1/navigation/location", status_code=202)
    async def navigation_location(
        payload: NavigationLocation,
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> dict[str, object]:
        value = Observation(**payload.model_dump(exclude={"navigation_session_id"}))
        return await navigation.process(identity.owner_id, payload.navigation_session_id, value)

    @app.post("/api/v1/navigation/stop")
    async def navigation_stop(
        payload: NavigationStop,
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> dict[str, bool]:
        await navigation.stop(identity.owner_id, payload.session_id)
        return {"stopped": True}

    @app.websocket("/api/v1/navigation/{session_id}/stream")
    async def navigation_stream(
        websocket: WebSocket,
        session_id: UUID,
        resume_sequence: int = Query(default=-1, ge=-1),
    ) -> None:
        if websocket.headers.get("origin") not in allowed:
            await websocket.close(code=4403)
            return
        token = websocket.cookies.get("mapinator_session")
        identity = (
            await asyncio.to_thread(repo.identity, _token_hash(token), datetime.now(UTC))
            if token
            else None
        )
        if identity is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        last_revision = -1
        last_route = ""
        heartbeat = time.monotonic()
        try:
            while True:
                if datetime.now(UTC) >= identity.expires_at:
                    await websocket.close(code=4401)
                    return
                view = await navigation.snapshot(identity.owner_id, session_id)
                revision = view["revision"]
                if not isinstance(revision, int):
                    raise DomainError(
                        503, "navigation_state_invalid", "Navigation revision invalid."
                    )
                if last_revision < 0 or revision > last_revision:
                    kind = (
                        "snapshot"
                        if last_revision < 0
                        else (
                            "arrived"
                            if view["state"] == "arrived"
                            else "route_updated"
                            if str(view["route_id"]) != last_route
                            else "eta_updated"
                        )
                    )
                    await asyncio.wait_for(websocket.send_json({"type": kind, **view}), timeout=2)
                    last_revision, last_route = revision, str(view["route_id"])
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 10:
                    await asyncio.wait_for(
                        websocket.send_json({"type": "heartbeat", "revision": revision}), timeout=2
                    )
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
        except (DomainError, SQLAlchemyError) as exc:
            code = exc.code if isinstance(exc, DomainError) else "database_unavailable"
            try:
                await asyncio.wait_for(
                    websocket.send_json({"type": "error", "code": code}), timeout=2
                )
                await websocket.close(code=4404)
            except (WebSocketDisconnect, TimeoutError):
                return
        except TimeoutError:
            await websocket.close(code=1008)

    @app.post("/api/v1/traffic/consent")
    def traffic_consent(
        payload: TrafficConsentRequest, identity: Annotated[Identity, Depends(mutation_guard)]
    ) -> dict[str, bool]:
        repo.traffic_consent(
            identity.owner_id, payload.session_id, payload.enabled, datetime.now(UTC)
        )
        return {"traffic_consent": payload.enabled}

    @app.get("/health/traffic")
    def traffic_health(response: Response) -> dict[str, object]:
        state = repo.traffic_status(datetime.now(UTC))
        if state["status"] != "ready":
            response.status_code = 503
        return state

    @app.get("/api/v1/traffic/segments/{segment_id}")
    def traffic_segment(segment_id: str) -> dict[str, object]:
        from mapinator.routing import build_graph

        parts = segment_id.split(":")
        try:
            if len(parts) != 2:
                raise ValueError("Invalid segment ID")
            cell = (int(parts[0]), int(parts[1]))
        except ValueError as exc:
            raise DomainError(422, "invalid_segment", "Segment ID must be x:y integers.") from exc
        _, dataset = repo.routing_dataset()
        graph = build_graph(dataset)
        if cell not in graph.segments:
            raise DomainError(
                404, "segment_unavailable", "Segment does not exist in the active graph."
            )
        now = datetime.now(UTC)
        snapshot = repo.traffic_snapshot(dataset.checksum, now)
        items = []
        for edge in graph.edges.values():
            if edge.segment != cell or "car" not in edge.speeds:
                continue
            estimate = snapshot.estimates.get(edge.id)
            speed, source = (
                effective_speed(edge.speeds["car"], estimate, now)
                if estimate
                else (edge.speeds["car"], "baseline")
            )
            items.append(
                {
                    "edge_id": edge.id,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            graph.nodes[edge.source],
                            graph.nodes[edge.target],
                        ],
                    },
                    "baseline_speed_mps": edge.speeds["car"],
                    "effective_speed_mps": speed,
                    "source": source,
                    "estimate": asdict(estimate) if estimate else None,
                }
            )
        return {
            "segment_id": segment_id,
            "graph_version": dataset.checksum,
            "traffic_version": snapshot.version,
            "items": items,
        }

    @app.post("/api/v1/location", response_model=LocationAccepted, status_code=202)
    def update_location(
        payload: LocationRequest, identity: Annotated[Identity, Depends(mutation_guard)]
    ) -> LocationAccepted:
        observation = Observation(**payload.model_dump())
        accepted = repo.save_location(identity.owner_id, observation, datetime.now(UTC))
        return LocationAccepted(accepted=True, deduplicated=not accepted)

    @app.get("/api/v1/location/current", response_model=CurrentLocation)
    def current_location(identity: Annotated[Identity, Depends(authenticate)]) -> CurrentLocation:
        now = datetime.now(UTC)
        location = repo.current_location(identity.owner_id, now)
        if location is None:
            raise DomainError(404, "location_unavailable", "No unexpired location is available.")
        return CurrentLocation(
            event_id=location.event_id,
            session_id=location.session_id,
            sequence=location.sequence,
            timestamp=location.timestamp,
            latitude=location.latitude,
            longitude=location.longitude,
            accuracy_m=location.accuracy_m,
            heading_deg=location.heading_deg,
            speed_mps=location.speed_mps,
            freshness_s=max(0, (now - location.timestamp).total_seconds()),
        )

    @app.get("/api/v1/geocoding/search", response_model=SearchResponse)
    def search(
        q: Annotated[str, Query(min_length=2, max_length=200)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        cursor: Annotated[int, Query(ge=0, le=10_000)] = 0,
        bbox: str | None = None,
    ) -> SearchResponse:
        query = q.strip()
        if len(query) < 2:
            raise DomainError(422, "invalid_query", "Query must contain at least two characters.")
        bounds = _parse_bounds(bbox) if bbox is not None else None
        found = repo.search(query, bounds, limit + 1, cursor)
        return SearchResponse(
            items=[PlaceResponse.model_validate(hit) for hit in found[:limit]],
            next_cursor=cursor + limit if len(found) > limit else None,
        )

    @app.get("/api/v1/datasets/current", response_model=DatasetResponse)
    def current_dataset() -> DatasetResponse:
        dataset = repo.dataset()
        if dataset is None:
            raise DomainError(
                404, "dataset_not_imported", "Import an OSM extract before using the map."
            )
        return DatasetResponse.model_validate(dataset)

    def resolve(value: Coordinate | str) -> tuple[float, float]:
        if isinstance(value, Coordinate):
            return value.longitude, value.latitude
        hits = repo.search(value.strip(), None, 2, 0)
        if len(hits) != 1:
            raise DomainError(
                400,
                "ambiguous_geocoding",
                "Select explicit coordinates for unknown/ambiguous place.",
            )
        return hits[0].longitude, hits[0].latitude

    @app.post("/api/v1/routes", response_model=RouteResponse, status_code=201)
    def create_route(
        payload: RouteRequest, identity: Annotated[Identity, Depends(mutation_guard)]
    ) -> RouteResponse:
        if not routing_slots.acquire(blocking=False):
            raise DomainError(429, "routing_busy", "Routing pool is full; retry later.")
        submitted = False
        try:
            source, destination = resolve(payload.source), resolve(payload.destination)
            dataset_id, dataset = repo.routing_dataset()
            now = datetime.now(UTC)
            try:
                snapshot = repo.traffic_snapshot(dataset.checksum, now)
            except SQLAlchemyError as exc:
                logger.error(
                    json.dumps({"event": "traffic_unavailable", "error": type(exc).__name__})
                )
                snapshot = TrafficSnapshot(unavailable=True)
            future = routing_pool.submit(
                calculate,
                dataset,
                source,
                destination,
                payload.mode,
                payload.strategy,
                payload.alternatives,
                payload.preferences.avoid_tolls,
                payload.preferences.avoid_motorways,
                snapshot,
                now,
            )
            submitted = True
            future.add_done_callback(lambda _: routing_slots.release())
            try:
                result = future.result(timeout=8)
            except FutureTimeoutError as exc:
                future.cancel()
                raise DomainError(503, "routing_timeout", "Routing exceeded its deadline.") from exc
            identifier = repo.save_route(identity.owner_id, dataset_id, result, datetime.now(UTC))
            return RouteResponse.model_validate(
                {**result, "route_id": identifier, "dataset_id": dataset_id}
            )
        except BrokenProcessPool as exc:
            logger.error(json.dumps({"event": "routing_worker_failed"}))
            raise DomainError(
                503, "routing_worker_failed", "Routing worker failed; restart required."
            ) from exc
        finally:
            if not submitted:
                routing_slots.release()

    @app.get("/api/v1/routes/{route_id}", response_model=RouteResponse)
    def get_route(
        route_id: UUID, identity: Annotated[Identity, Depends(authenticate)]
    ) -> RouteResponse:
        result = repo.route(identity.owner_id, route_id, datetime.now(UTC))
        if result is None:
            raise DomainError(404, "route_unavailable", "Route is absent, inaccessible or expired.")
        return RouteResponse.model_validate(result)

    from mapinator.ev_api import create_ev_router

    app.include_router(create_ev_router(repo, mutation_guard, execute))
    from mapinator.discovery_api import create_discovery_router

    app.include_router(create_discovery_router(repo, mutation_guard, interpreter, conversations))

    static_path = str(files("mapinator").joinpath("static"))
    app.mount("/", StaticFiles(directory=static_path, html=True), name="web")
    return app


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse_bounds(raw: str) -> tuple[float, float, float, float]:
    import math

    try:
        parts = [float(part) for part in raw.split(",")]
    except ValueError as exc:
        raise DomainError(422, "invalid_bbox", "bbox must contain west,south,east,north.") from exc
    if len(parts) != 4:
        raise DomainError(422, "invalid_bbox", "bbox must contain four coordinates.")
    west, south, east, north = parts
    if not all(math.isfinite(p) for p in parts) or not (
        -180 <= west < east <= 180 and -90 <= south < north <= 90
    ):
        raise DomainError(422, "invalid_bbox", "bbox bounds must be ordered WGS84 coordinates.")
    return west, south, east, north
