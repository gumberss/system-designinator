import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from mapinator.domain import DomainError, Observation, Repository
from mapinator.routing import Mode, Strategy, distance

logger = logging.getLogger("mapinator")
Point = tuple[float, float]
Recalculate = Callable[
    [UUID, Point, Point, Mode, Strategy, bool, bool], Awaitable[dict[str, object]]
]


class NavigationState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    revision: int = Field(default=0, ge=0)
    route_id: UUID
    state: Literal["awaiting_location", "navigating", "off_route", "arrived"] = "awaiting_location"
    last_sequence: int = -1
    last_event_id: UUID | None = None
    last_digest: str = ""
    progress_m: float = 0
    remaining_m: float = 0
    eta_s: float | None = None
    off_route_samples: int = 0
    last_reroute_at: float | None = None
    warnings: list[str] = Field(default_factory=list)


class NavigationCache(Protocol):
    async def health(self) -> None: ...
    async def get(self, owner: UUID, session: UUID) -> NavigationState | None: ...
    async def put(
        self, owner: UUID, session: UUID, state: NavigationState, expected: int
    ) -> bool: ...
    async def delete(self, owner: UUID, session: UUID) -> None: ...
    async def close(self) -> None: ...


class RedisNavigationCache:
    def __init__(self, url: str) -> None:
        self.client = Redis.from_url(
            url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )

    def key(self, owner: UUID, session: UUID) -> str:
        return f"mapinator:navigation:{owner}:{session}"

    async def health(self) -> None:
        try:
            await self.client.ping()
        except RedisError as exc:
            raise DomainError(
                503, "navigation_cache_unavailable", "Redis navigation unavailable."
            ) from exc

    async def get(self, owner: UUID, session: UUID) -> NavigationState | None:
        try:
            raw = await self.client.get(self.key(owner, session))
            return NavigationState.model_validate_json(raw) if raw is not None else None
        except (RedisError, ValidationError) as exc:
            logger.error(
                json.dumps({"event": "navigation_cache_error", "error": type(exc).__name__})
            )
            raise DomainError(
                503, "navigation_cache_unavailable", "Navigation state unavailable."
            ) from exc

    async def put(self, owner: UUID, session: UUID, state: NavigationState, expected: int) -> bool:
        script = """
        local raw=redis.call('GET',KEYS[1])
        if raw then
            if cjson.decode(raw).revision~=tonumber(ARGV[1]) then return 0 end
        elseif tonumber(ARGV[1])~=-1 then return 0 end
        redis.call('SET',KEYS[1],ARGV[2],'EX',300)
        return 1
        """
        try:
            result = await self.client.eval(
                script, 1, self.key(owner, session), expected, state.model_dump_json()
            )
            if not isinstance(result, int) or result not in (0, 1):
                raise DomainError(
                    503, "navigation_cache_invalid", "Redis CAS returned an invalid result."
                )
            return result == 1
        except RedisError as exc:
            raise DomainError(
                503, "navigation_cache_unavailable", "Navigation update unavailable."
            ) from exc

    async def delete(self, owner: UUID, session: UUID) -> None:
        try:
            await self.client.delete(self.key(owner, session))
        except RedisError as exc:
            raise DomainError(
                503, "navigation_cache_unavailable", "Navigation cache deletion unconfirmed."
            ) from exc

    async def close(self) -> None:
        await self.client.aclose()


class Geometry(BaseModel):
    coordinates: list[Point] = Field(min_length=2)


class Path(BaseModel):
    geometry: Geometry
    distance_m: float
    eta_s: float
    edge_durations_s: list[float] = Field(default_factory=list)
    mode: Mode
    strategy: Strategy
    instructions: list[dict[str, object]]
    preferences: dict[str, bool] = Field(default_factory=dict)


def project_path(points: list[Point], point: Point, progress: float = 0) -> tuple[float, float]:
    best = (math.inf, progress)
    travelled = 0.0
    for a, b in zip(points, points[1:], strict=False):
        length = distance(a, b)
        if length and travelled + length >= progress - 20:
            scale = math.cos(math.radians(point[1]))
            dx, dy = (b[0] - a[0]) * scale, b[1] - a[1]
            fraction = max(
                0.0,
                min(
                    1.0,
                    ((point[0] - a[0]) * scale * dx + (point[1] - a[1]) * dy) / (dx * dx + dy * dy),
                ),
            )
            projected = (a[0] + fraction * (b[0] - a[0]), a[1] + fraction * (b[1] - a[1]))
            candidate = (distance(point, projected), travelled + fraction * length)
            if candidate < best:
                best = candidate
        travelled += length
    if travelled == 0:
        return distance(point, points[0]), 0
    return best[0], max(progress, best[1])


def remaining_eta(path: Path, progress: float) -> float:
    lengths = [
        distance(a, b)
        for a, b in zip(path.geometry.coordinates, path.geometry.coordinates[1:], strict=False)
    ]
    total = sum(lengths)
    durations = path.edge_durations_s
    if len(durations) != len(lengths):
        return path.eta_s * max(0, 1 - progress / total) if total else 0
    travelled = 0.0
    eta = 0.0
    for length, duration in zip(lengths, durations, strict=True):
        fraction = min(1, max(0, (progress - travelled) / length)) if length else 1
        eta += duration * (1 - fraction)
        travelled += length
    return eta


class NavigationService:
    def __init__(
        self, repo: Repository, cache: NavigationCache | None, recalculate: Recalculate
    ) -> None:
        self.repo, self.cache, self.recalculate = repo, cache, recalculate

    def required_cache(self) -> NavigationCache:
        if self.cache is None:
            raise DomainError(
                503, "navigation_not_configured", "Configure Redis before navigation."
            )
        return self.cache

    async def start(
        self,
        owner: UUID,
        route_id: UUID,
        tracking_id: UUID,
        traffic_consent: bool,
    ) -> dict[str, object]:
        cache = self.required_cache()
        await cache.health()
        now = datetime.now(UTC)
        route = await asyncio.to_thread(self.repo.route, owner, route_id, now)
        if route is None:
            raise DomainError(404, "route_unavailable", "Owned active route required.")
        path = Path.model_validate(route)
        if "preferences" not in route:
            raise DomainError(
                422, "route_preferences_unknown", "Recalculate this legacy route before navigation."
            )
        if traffic_consent and path.mode != "car":
            raise DomainError(
                422, "traffic_mode_invalid", "Traffic contribution requires car mode."
            )
        previous = await asyncio.to_thread(self.repo.navigation, owner, None, now)
        record = await asyncio.to_thread(
            self.repo.navigation_start, owner, tracking_id, route_id, traffic_consent, now
        )
        state = NavigationState(route_id=route_id, remaining_m=path.distance_m, eta_s=path.eta_s)
        try:
            if not await cache.put(owner, record.session_id, state, -1):
                raise DomainError(409, "navigation_conflict", "Navigation state already exists.")
            if previous:
                await cache.delete(owner, previous.session_id)
        except DomainError:
            try:
                await asyncio.to_thread(self.repo.stop_tracking, owner)
            except SQLAlchemyError as exc:
                logger.error(
                    json.dumps({"event": "navigation_rollback_failed", "error": type(exc).__name__})
                )
                raise DomainError(
                    503, "navigation_cleanup_unconfirmed", "Start failed; explicitly stop tracking."
                ) from exc
            raise
        return await self.snapshot(owner, record.session_id)

    async def snapshot(self, owner: UUID, session: UUID) -> dict[str, object]:
        record = await asyncio.to_thread(self.repo.navigation, owner, session, datetime.now(UTC))
        if record is None:
            raise DomainError(
                404, "navigation_unavailable", "Navigation stopped, expired or inaccessible."
            )
        state = await self.required_cache().get(owner, session)
        if state is None:
            raise DomainError(
                410,
                "navigation_expired",
                "State expired or Redis restarted; start navigation again.",
            )
        route = await asyncio.to_thread(self.repo.route, owner, state.route_id, datetime.now(UTC))
        if route is None:
            raise DomainError(410, "navigation_route_expired", "Navigation route expired.")
        path = Path.model_validate(route)
        cumulative = 0.0
        instruction: dict[str, object] | None = None
        for step in path.instructions:
            length = step.get("distance_m", 0)
            if isinstance(length, (int, float)):
                cumulative += length
            if cumulative >= state.progress_m or step.get("action") == "arrive":
                instruction = step
                break
        return {
            "session_id": str(session),
            "tracking_session_id": str(record.tracking_session_id),
            "revision": state.revision,
            "state": state.state,
            "route_id": str(state.route_id),
            "progress_m": state.progress_m,
            "remaining_m": state.remaining_m,
            "eta_s": state.eta_s,
            "warnings": state.warnings,
            "instruction": instruction,
            "route": route,
        }

    async def process(self, owner: UUID, session: UUID, value: Observation) -> dict[str, object]:
        record = await asyncio.to_thread(self.repo.navigation, owner, session, datetime.now(UTC))
        if record is None or record.tracking_session_id != value.session_id:
            raise DomainError(
                403, "navigation_not_allowed", "Owned active navigation/tracking required."
            )
        cache = self.required_cache()
        state = await cache.get(owner, session)
        if state is None:
            raise DomainError(
                410, "navigation_expired", "Navigation state expired; restart explicitly."
            )
        digest = hashlib.sha256(repr(value).encode()).hexdigest()
        if value.sequence <= state.last_sequence:
            if value.event_id == state.last_event_id and digest == state.last_digest:
                return await self.snapshot(owner, session)
            raise DomainError(409, "stale_navigation", "Observation already superseded.")
        await asyncio.to_thread(self.repo.save_location, owner, value, datetime.now(UTC))
        route = await asyncio.to_thread(self.repo.route, owner, state.route_id, datetime.now(UTC))
        if route is None:
            raise DomainError(410, "navigation_route_expired", "Navigation route expired.")
        path = Path.model_validate(route)
        updated = state.model_copy(deep=True)
        updated.revision += 1
        updated.last_sequence, updated.last_event_id, updated.last_digest = (
            value.sequence,
            value.event_id,
            digest,
        )
        point = (value.longitude, value.latitude)
        if value.accuracy_m > 35:
            updated.warnings = ["GPS accuracy insufficient; progress/ETA not refreshed."]
            updated.off_route_samples = 0
        elif state.state != "arrived":
            error, progress = project_path(path.geometry.coordinates, point, state.progress_m)
            if error > max(30, value.accuracy_m * 2):
                updated.off_route_samples += 1
                updated.state, updated.eta_s = "off_route", None
                now = datetime.now(UTC).timestamp()
                if updated.off_route_samples >= 2 and (
                    state.last_reroute_at is None or now - state.last_reroute_at >= 30
                ):
                    updated.last_reroute_at = now
                    try:
                        rerouted = await self.recalculate(
                            owner,
                            point,
                            path.geometry.coordinates[-1],
                            path.mode,
                            path.strategy,
                            path.preferences.get("avoid_tolls", False),
                            path.preferences.get("avoid_motorways", False),
                        )
                    except DomainError as exc:
                        if exc.status not in (422, 429, 503):
                            raise
                        updated.warnings = [
                            f"Reroute unavailable: {exc.code}; no replacement route."
                        ]
                        logger.warning(
                            json.dumps({"event": "navigation_reroute_failed", "code": exc.code})
                        )
                    else:
                        new_path = Path.model_validate(rerouted)
                        identifier = rerouted.get("route_id")
                        if not isinstance(identifier, (str, UUID)):
                            raise DomainError(
                                503, "reroute_invalid", "Reroute has no persisted route ID."
                            )
                        updated.route_id = UUID(str(identifier))
                        updated.progress_m, updated.remaining_m, updated.eta_s = (
                            0,
                            new_path.distance_m,
                            new_path.eta_s,
                        )
                        updated.off_route_samples, updated.state, updated.warnings = (
                            0,
                            "navigating",
                            [],
                        )
            else:
                if error <= max(15, value.accuracy_m):
                    updated.off_route_samples = 0
                updated.progress_m = min(path.distance_m, progress)
                updated.remaining_m = max(0, path.distance_m - updated.progress_m)
                updated.eta_s = remaining_eta(path, updated.progress_m)
                updated.state, updated.warnings = "navigating", []
                if (
                    updated.remaining_m <= 25
                    and distance(point, path.geometry.coordinates[-1]) <= 15
                ):
                    updated.state, updated.eta_s = "arrived", 0
        current = await asyncio.to_thread(self.repo.current_location, owner, datetime.now(UTC))
        if current is None or current.event_id != value.event_id:
            raise DomainError(
                409, "navigation_superseded", "A newer position or stop superseded this update."
            )
        if not await cache.put(owner, session, updated, state.revision):
            raise DomainError(
                409, "navigation_conflict", "Concurrent navigation update; send next observation."
            )
        return await self.snapshot(owner, session)

    async def stop(self, owner: UUID, session: UUID | None = None) -> None:
        record = await asyncio.to_thread(self.repo.navigation, owner, session, datetime.now(UTC))
        if session is None or record is not None:
            await asyncio.to_thread(self.repo.stop_tracking, owner)
        if self.cache is not None:
            target = record.session_id if record else session
            if target is not None:
                await self.cache.delete(owner, target)
