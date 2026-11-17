import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError

from mapinator.domain import Identity, Repository
from mapinator.ev import StrictModel, VehicleRequest, plan_ev_trip
from mapinator.traffic import TrafficSnapshot


class EVCoordinate(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class EVTripRequest(StrictModel):
    source: EVCoordinate
    destination: EVCoordinate
    vehicle: VehicleRequest | None = None


def create_ev_router(
    repo: Repository,
    mutation_guard: Callable[..., Identity],
    execute: Callable[[Callable[[], dict[str, object]]], Awaitable[dict[str, object]]],
) -> APIRouter:
    """execute owns the bounded ProcessPool, admission limits and worker failure handling."""
    router = APIRouter()

    @router.post("/api/v1/ev/trips", status_code=201)
    async def create_trip(
        payload: EVTripRequest,
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> dict[str, object]:
        dataset_id, dataset = await asyncio.to_thread(repo.routing_dataset)
        now = datetime.now(UTC)
        try:
            snapshot = await asyncio.to_thread(repo.traffic_snapshot, dataset.checksum, now)
        except SQLAlchemyError:
            snapshot = TrafficSnapshot(unavailable=True)
        result = await execute(
            partial(
                plan_ev_trip,
                dataset,
                (payload.source.longitude, payload.source.latitude),
                (payload.destination.longitude, payload.destination.latitude),
                payload.vehicle,
                snapshot,
                now,
            )
        )
        # Only the unchanged calculate result is a normal route. EV legs/wrapper must not
        # be persisted through save_route or exposed as a navigable continuous route.
        if result["ev_planning_available"] is False:
            route = cast(dict[str, object], result["ordinary_route"])
            identifier = await asyncio.to_thread(
                repo.save_route,
                identity.owner_id,
                dataset_id,
                route,
                datetime.now(UTC),
            )
            result["ordinary_route"] = {
                **route,
                "route_id": str(identifier),
                "dataset_id": str(dataset_id),
            }
        return {**result, "dataset_id": str(dataset_id)}

    return router
