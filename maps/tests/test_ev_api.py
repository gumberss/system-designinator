import asyncio
import pickle
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from mapinator.domain import DomainError, Identity
from mapinator.ev import plan_ev_trip
from mapinator.ev_api import create_ev_router
from mapinator.traffic import TrafficSnapshot
from tests.memory import MemoryRepository
from tests.test_ev import profile, regional_fixture


def payload(with_vehicle: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "source": {"latitude": 0.0, "longitude": 0.0},
        "destination": {"latitude": 0.0, "longitude": 0.3},
    }
    if with_vehicle:
        value["vehicle"] = profile().model_dump()
    return value


def test_authenticated_callbacks_processpool_and_no_ev_wrapper_persistence() -> None:
    repo = MemoryRepository()
    repo.road_data = regional_fixture()
    identity = Identity(uuid4(), "synthetic-csrf", datetime.now(UTC) + timedelta(hours=1))
    calls: list[Callable[[], dict[str, object]]] = []
    guards: list[Identity] = []
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status)

    def guard(request: Request) -> Identity:
        if request.headers.get("authorization") != "Bearer synthetic-owner":
            raise DomainError(401, "authentication_required", "Synthetic identity missing.")
        if request.headers.get("x-csrf-token") != identity.csrf_token:
            raise DomainError(403, "csrf_failed", "Synthetic CSRF mismatch.")
        guards.append(identity)
        return identity

    with ProcessPoolExecutor(max_workers=1) as pool:

        async def execute(work: Callable[[], dict[str, object]]) -> dict[str, object]:
            assert isinstance(work, partial) and work.func is plan_ev_trip
            pickle.dumps(work)
            calls.append(work)
            return await asyncio.get_running_loop().run_in_executor(pool, work)

        app.include_router(create_ev_router(repo, guard, execute))
        headers = {
            "authorization": "Bearer synthetic-owner",
            "x-csrf-token": identity.csrf_token,
        }
        with TestClient(app) as client:
            assert client.post("/api/v1/ev/trips", json=payload()).status_code == 401
            assert (
                client.post(
                    "/api/v1/ev/trips",
                    json=payload(),
                    headers={"authorization": "Bearer synthetic-owner"},
                ).status_code
                == 403
            )
            response = client.post("/api/v1/ev/trips", json=payload(), headers=headers)
            assert response.status_code == 201, response.text
            result = response.json()
            assert result["ev_planning_available"] is True and result["feasible"] is True
            assert result["ordinary_route"] is None
            assert len(result["legs"]) == 3 and len(result["charging_stops"]) == 2
            assert result["dataset_id"] and result["provenance"]
            assert result["charging_stops"][0]["availability"] == "unknown"
            assert result["charging_stops"][0]["coordinate"] == {
                "latitude": 0.0,
                "longitude": 0.1,
            }
            assert result["legs"][0]["source"] == {"latitude": 0.0, "longitude": 0.0}
            assert result["legs"][-1]["destination"] == {
                "latitude": 0.0,
                "longitude": 0.3,
            }
            assert result["energy"]["upper_kwh"] > result["energy"]["lower_kwh"]
            assert repo.routes == {}
            fallback = client.post("/api/v1/ev/trips", json=payload(False), headers=headers)
            assert fallback.status_code == 201, fallback.text
            ordinary = fallback.json()
            assert ordinary["ev_planning_available"] is False and ordinary["feasible"] is None
            assert ordinary["charging_stops"] == [] and ordinary["ordinary_route"]["route_id"]
            saved_owner, saved, _ = next(iter(repo.routes.values()))
            assert saved_owner == identity.owner_id and saved["algorithm"] == "hierarchical"
            assert "ev_planning_available" not in saved
            assert len(calls) == 2 and len(guards) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"source": {"latitude": 91.0, "longitude": 0.0}},
        {"source": {"lat": 0.0, "lon": 0.0}},
        {"vehicle": {"battery_capacity_kwh": -1.0}},
        {"vehicle": {"battery_capacity_kwh": "50"}},
        {"vehicle": {"connectors": ["invented"]}},
        {"vehicle": {"consumption_kwh_per_100km": 0.0}},
        {"synthetic_station": {"available": True}},
    ],
)
def test_request_validation_does_not_execute_work(change: dict[str, object]) -> None:
    repo = MemoryRepository()
    repo.road_data = regional_fixture()
    app = FastAPI()

    def guard() -> Identity:
        return Identity(uuid4(), "synthetic", datetime.now(UTC) + timedelta(hours=1))

    async def execute(work: Callable[[], dict[str, object]]) -> dict[str, object]:
        pytest.fail("Invalid input must not reach CPU execution.")

    app.include_router(create_ev_router(repo, guard, execute))
    with TestClient(app) as client:
        assert client.post("/api/v1/ev/trips", json={**payload(), **change}).status_code == 422
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/api/v1/ev/trips"]["post"]["responses"]["201"]


def test_domain_errors_and_unavailable_traffic_remain_explicit() -> None:
    class UnavailableTraffic(MemoryRepository):
        def traffic_snapshot(self, graph_version: str, now: datetime) -> TrafficSnapshot:
            raise OperationalError("synthetic traffic outage", None, Exception("unavailable"))

    repo = UnavailableTraffic()
    repo.road_data = regional_fixture()
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"code": exc.code}, status_code=exc.status)

    def guard() -> Identity:
        return Identity(uuid4(), "synthetic", datetime.now(UTC) + timedelta(hours=1))

    async def execute(work: Callable[[], dict[str, object]]) -> dict[str, object]:
        return await asyncio.to_thread(work)

    app.include_router(create_ev_router(repo, guard, execute))
    with TestClient(app) as client:
        result = client.post("/api/v1/ev/trips", json=payload(False))
        assert result.status_code == 201
        assert any(
            "Traffic storage unavailable" in w for w in result.json()["ordinary_route"]["warnings"]
        )
        impossible = payload()
        impossible["vehicle"] = profile(initial_soc_percent=60.0).model_dump()
        failed = client.post("/api/v1/ev/trips", json=impossible)
        assert failed.status_code == 422 and failed.json()["code"] == "ev_trip_unreachable"
