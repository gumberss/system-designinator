import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path as FilePath
from uuid import UUID, uuid4

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from mapinator.api import Settings, create_app
from mapinator.domain import DomainError, Observation
from mapinator.navigation import (
    NavigationService,
    Path,
    RedisNavigationCache,
    project_path,
    remaining_eta,
)
from mapinator.routing import Mode, Strategy, calculate
from tests.memory import MemoryRepository
from tests.navigation_memory import MemoryNavigationCache
from tests.test_api import ORIGIN, enable_tracking, sign_in
from tests.test_routing import network


def test_projection_not_vertices_and_weighted_eta() -> None:
    error, progress = project_path([(0, 0), (0.004, 0)], (0.002, 0))
    assert error < 0.001 and 222 < progress < 223
    path = Path.model_validate(
        {
            "geometry": {"coordinates": [[0, 0], [0.002, 0], [0.004, 0]]},
            "distance_m": 444.8,
            "eta_s": 110,
            "edge_durations_s": [100, 10],
            "mode": "car",
            "strategy": "fastest",
            "instructions": [],
        }
    )
    assert remaining_eta(path, progress) == pytest.approx(10, abs=0.01)
    assert project_path([(0, 0), (0, 0)], (0, 0)) == (0, 0)


def test_progress_duplicates_expiry_stop_and_reroute_preferences() -> None:
    async def verify() -> None:
        repo, cache = MemoryRepository(), MemoryNavigationCache()
        owner = uuid4()
        now = datetime.now(UTC)
        repo.road_data = network(
            [(0, 0), (0.004, 0), (0, 0.001), (0.004, 0.001)],
            [(1, 2, {}), (1, 3, {}), (3, 4, {}), (4, 2, {})],
        )
        dataset_id, dataset = repo.routing_dataset()
        tracking = repo.start_tracking(owner, now)
        initial = calculate(dataset, (0, 0), (0.004, 0), "car", "fastest", 0, True, True)
        route_id = repo.save_route(owner, dataset_id, initial, now)
        calls: list[tuple[bool, bool]] = []

        async def recalc(
            owner_id: UUID,
            source: tuple[float, float],
            goal: tuple[float, float],
            mode: Mode,
            strategy: Strategy,
            tolls: bool,
            motorways: bool,
        ) -> dict[str, object]:
            calls.append((tolls, motorways))
            result = calculate(dataset, source, goal, mode, strategy, 0, tolls, motorways)
            identifier = repo.save_route(owner_id, dataset_id, result, datetime.now(UTC))
            return {**result, "route_id": str(identifier), "dataset_id": str(dataset_id)}

        service = NavigationService(repo, cache, recalc)
        view = await service.start(owner, route_id, tracking.session_id, True)
        session = UUID(str(view["session_id"]))
        sample = Observation(uuid4(), tracking.session_id, 1, now, 0, 0.002, 5, 90, 3)
        view = await service.process(owner, session, sample)
        assert 222 < float(str(view["progress_m"])) < 223
        assert (await service.process(owner, session, sample))["revision"] == 1
        for sequence in (2, 3):
            sample = Observation(
                uuid4(),
                tracking.session_id,
                sequence,
                now + timedelta(seconds=sequence),
                0.001,
                0.002,
                5,
                90,
                3,
            )
            view = await service.process(owner, session, sample)
        assert calls == [(True, True)] and view["route_id"] != str(route_id)
        assert view["state"] == "navigating"
        with pytest.raises(DomainError, match="superseded"):
            await service.process(
                owner,
                session,
                sample.__class__(uuid4(), tracking.session_id, 1, now, 0, 0.002, 5, 90, 3),
            )
        record = cache.records[owner, session]
        cache.records[owner, session] = record[0], now - timedelta(seconds=1)
        with pytest.raises(DomainError, match="expired"):
            await service.snapshot(owner, session)
        await service.stop(owner, session)
        await service.stop(owner, session)
        assert not repo.tracking and not repo.consent and not cache.records

    asyncio.run(verify())


def test_accuracy_reroute_failure_cooldown_and_arrival() -> None:
    async def verify() -> None:
        repo, cache = MemoryRepository(), MemoryNavigationCache()
        owner, now = uuid4(), datetime.now(UTC)
        repo.road_data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
        dataset_id, dataset = repo.routing_dataset()
        tracking = repo.start_tracking(owner, now)
        route = repo.save_route(
            owner, dataset_id, calculate(dataset, (0, 0), (0.004, 0), "car", "fastest"), now
        )
        calls = []

        async def failure(
            owner_id: UUID,
            source: tuple[float, float],
            goal: tuple[float, float],
            mode: Mode,
            strategy: Strategy,
            tolls: bool,
            motorways: bool,
        ) -> dict[str, object]:
            calls.append(True)
            raise DomainError(422, "route_not_found", "Synthetic unavailable replacement.")

        service = NavigationService(repo, cache, failure)
        session = UUID(
            str((await service.start(owner, route, tracking.session_id, False))["session_id"])
        )
        for seq in range(1, 6):
            if seq in (4, 5):
                state, expiry = cache.records[owner, session]
                state.last_reroute_at = datetime.now(UTC).timestamp() - (29 if seq == 4 else 31)
            sample = Observation(
                uuid4(),
                tracking.session_id,
                seq,
                now + timedelta(seconds=seq),
                0.001,
                0.002,
                80 if seq == 1 else 5,
                None,
                None,
            )
            view = await service.process(owner, session, sample)
            if seq == 1:
                assert view["state"] == "awaiting_location" and not calls
            if seq == 3:
                assert view["state"] == "off_route" and view["eta_s"] is None and len(calls) == 1
                assert "route_not_found" in str(view["warnings"])
            if seq == 4:
                assert len(calls) == 1
        assert len(calls) == 2
        destination = Observation(
            uuid4(), tracking.session_id, 6, now + timedelta(seconds=6), 0, 0.004, 5, None, None
        )
        arrived = await service.process(owner, session, destination)
        assert (
            arrived["state"] == "arrived" and arrived["eta_s"] == 0 and arrived["remaining_m"] == 0
        )

    asyncio.run(verify())


def test_http_ws_origin_ownership_resume_and_revocation() -> None:
    repo, cache = MemoryRepository(), MemoryNavigationCache()
    repo.road_data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN), cache)) as client:
        headers = sign_in(client)
        tracking = enable_tracking(client, headers)
        route = client.post(
            "/api/v1/routes",
            headers=headers,
            json={
                "source": {"longitude": 0, "latitude": 0},
                "destination": {"longitude": 0.004, "latitude": 0},
            },
        ).json()
        response = client.post(
            "/api/v1/navigation/start",
            headers=headers,
            json={
                "route_id": route["route_id"],
                "tracking_session_id": tracking,
                "tracking_consent": True,
                "traffic_consent": False,
            },
        )
        assert response.status_code == 201
        session = response.json()["session_id"]
        path = f"/api/v1/navigation/{session}/stream"
        with client.websocket_connect(path, headers={"origin": ORIGIN}) as ws:
            assert ws.receive_json()["type"] == "snapshot"
            sample = {
                "event_id": str(uuid4()),
                "session_id": tracking,
                "navigation_session_id": session,
                "sequence": 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "longitude": 0.002,
                "latitude": 0,
                "accuracy_m": 5,
            }
            assert (
                client.post("/api/v1/navigation/location", headers=headers, json=sample).status_code
                == 202
            )
            assert ws.receive_json()["type"] == "eta_updated"
        with client.websocket_connect(
            path + "?resume_sequence=1", headers={"origin": ORIGIN}
        ) as ws:
            assert ws.receive_json()["revision"] == 1
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(path, headers={"origin": "https://evil.invalid"}):
                pytest.fail("Cross-origin connection accepted.")
        assert denied.value.code == 4403
        original_cookie = client.cookies.get("mapinator_session")
        assert original_cookie is not None
        client.cookies.clear()
        other_headers = sign_in(client)
        with client.websocket_connect(path, headers={"origin": ORIGIN}) as ws:
            assert ws.receive_json() == {"type": "error", "code": "navigation_unavailable"}
        assert (
            client.post(
                "/api/v1/navigation/location", headers=other_headers, json=sample
            ).status_code
            == 403
        )
        client.cookies.clear()
        client.cookies.set("mapinator_session", original_cookie)
        assert (
            client.post(
                "/api/v1/navigation/stop", headers=headers, json={"session_id": session}
            ).status_code
            == 200
        )
        assert not repo.tracking and not cache.records
        assert (
            client.post("/api/v1/navigation/location", headers=headers, json=sample).status_code
            == 403
        )
        assert client.get("/health/navigation").status_code == 200
        cache.available = False
        assert client.get("/health/navigation").status_code == 503
        assert client.get("/health/ready").status_code == 200


@pytest.mark.integration
def test_real_redis_cas_ttl_and_owner_isolation() -> None:
    url = os.environ.get("MAPINATOR_TEST_REDIS_URL")
    if not url:
        pytest.skip("Requires an isolated disposable Redis endpoint.")

    async def verify() -> None:
        from mapinator.navigation import NavigationState

        cache = RedisNavigationCache(url)
        owner, other, session = uuid4(), uuid4(), uuid4()
        try:
            state = NavigationState(route_id=uuid4())
            assert await cache.put(owner, session, state, -1)
            assert await cache.get(other, session) is None
            assert not await cache.put(owner, session, state, -1)
            assert 0 < await cache.client.ttl(cache.key(owner, session)) <= 300
            await cache.client.expire(cache.key(owner, session), 0)
            assert await cache.get(owner, session) is None
        finally:
            await cache.delete(owner, session)
            await cache.close()

    asyncio.run(verify())


@pytest.mark.integration
def test_real_multi_instance_navigation_sql_redis(tmp_path: FilePath) -> None:
    url = os.environ.get("MAPINATOR_TEST_DATABASE_URL")
    redis_url = os.environ.get("MAPINATOR_TEST_REDIS_URL")
    if not url or not redis_url:
        pytest.skip("Requires isolated disposable PostGIS and Redis for two application instances.")
    from sqlalchemy import text

    from mapinator.osm import parse_osm
    from mapinator.postgres import PostgresRepository, import_dataset, make_engine, migrate

    engines = [make_engine(url), make_engine(url)]
    repositories = [PostgresRepository(engine) for engine in engines]
    path = tmp_path / "synthetic-multi-instance.osm"
    path.write_text(
        '<osm><node id="1" lat="0" lon="0"/><node id="2" lat="0" lon="0.004"/>'
        '<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>'
    )
    try:
        migrate(engines[0])
        import_dataset(
            engines[0], parse_osm(path), "https://example.invalid/synthetic-multi-instance"
        )
        apps = [
            create_app(repo, Settings(allowed_origins=ORIGIN), RedisNavigationCache(redis_url))
            for repo in repositories
        ]
        with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
            headers = sign_in(first)
            second.cookies.update(first.cookies)
            tracking = enable_tracking(first, headers)
            route = first.post(
                "/api/v1/routes",
                headers=headers,
                json={
                    "source": {"latitude": 0, "longitude": 0},
                    "destination": {"latitude": 0, "longitude": 0.004},
                },
            ).json()
            response = first.post(
                "/api/v1/navigation/start",
                headers=headers,
                json={
                    "route_id": route["route_id"],
                    "tracking_session_id": tracking,
                    "tracking_consent": True,
                    "traffic_consent": False,
                },
            )
            assert response.status_code == 201
            session = response.json()["session_id"]
            with second.websocket_connect(
                f"/api/v1/navigation/{session}/stream", headers={"origin": ORIGIN}
            ) as ws:
                assert ws.receive_json()["type"] == "snapshot"
                sample = {
                    "event_id": str(uuid4()),
                    "session_id": tracking,
                    "navigation_session_id": session,
                    "sequence": 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "latitude": 0,
                    "longitude": 0.002,
                    "accuracy_m": 5,
                }
                assert (
                    first.post(
                        "/api/v1/navigation/location", headers=headers, json=sample
                    ).status_code
                    == 202
                )
                assert ws.receive_json()["type"] == "eta_updated"
            assert (
                second.post(
                    "/api/v1/navigation/stop", headers=headers, json={"session_id": session}
                ).status_code
                == 200
            )
            assert first.get("/api/v1/location/current").status_code == 404
            with engines[0].connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM navigation_sessions "
                            "WHERE session_id=CAST(:id AS uuid)"
                        ),
                        {"id": session},
                    ).scalar_one()
                    == 0
                )
    finally:
        for engine in engines:
            engine.dispose()
