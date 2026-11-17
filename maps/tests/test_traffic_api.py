from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from mapinator.api import Settings, create_app
from mapinator.traffic import TrafficEstimate, TrafficSnapshot
from tests.memory import MemoryRepository
from tests.test_api import ORIGIN, enable_tracking, sign_in
from tests.test_routing import network


@pytest.fixture
def repo() -> MemoryRepository:
    value = MemoryRepository()
    value.road_data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
    return value


@pytest.fixture
def client(repo: MemoryRepository) -> Iterator[TestClient]:
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as value:
        yield value


def test_consent_is_opt_in_owner_scoped_and_revoked(
    client: TestClient, repo: MemoryRepository
) -> None:
    headers = sign_in(client)
    session = enable_tracking(client, headers)
    assert not repo.consent
    payload = {"session_id": session, "enabled": True, "mode": "car"}
    assert client.post("/api/v1/traffic/consent", json=payload, headers=headers).status_code == 200
    assert uuid4() not in repo.consent and repo.consent
    assert (
        client.post(
            "/api/v1/traffic/consent", json={**payload, "session_id": str(uuid4())}, headers=headers
        ).status_code
        == 403
    )
    assert client.post("/api/v1/traffic/consent", json=payload).status_code == 403
    assert client.delete("/api/v1/location/current", headers=headers).status_code == 200
    assert not repo.consent


def test_traffic_segment_and_degraded_health(client: TestClient, repo: MemoryRepository) -> None:
    now = datetime.now(UTC)
    repo.snapshot = TrafficSnapshot(
        "unit-test",
        {
            "10:0:0:1": TrafficEstimate("10:0:0:1", 2, now, 1, 10, "heavy"),
        },
    )
    data = client.get("/api/v1/traffic/segments/0:0").json()
    assert data["traffic_version"] == "unit-test"
    assert any(item["source"] == "realtime" for item in data["items"])
    edge = next(item for item in data["items"] if item["edge_id"] == "10:0:0:1")
    assert edge["geometry"] == {"type": "LineString", "coordinates": [[0, 0], [0.004, 0]]}
    assert edge["baseline_speed_mps"] > edge["effective_speed_mps"] > 0
    assert client.get("/api/v1/traffic/segments/invalid").status_code == 422
    assert client.get("/api/v1/traffic/segments/100:100").status_code == 404
    assert client.get("/health/traffic").status_code == 503
    assert client.get("/health/ready").status_code == 200


def test_optional_traffic_store_failure_does_not_break_routes() -> None:
    class FailingTraffic(MemoryRepository):
        def traffic_snapshot(self, graph_version: str, now: datetime) -> TrafficSnapshot:
            raise OperationalError("traffic query", {}, Exception("traffic-only failure"))

    repo = FailingTraffic()
    repo.road_data = network([(0, 0), (0.004, 0)], [(1, 2, {})])
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as client:
        headers = sign_in(client)
        response = client.post(
            "/api/v1/routes",
            headers=headers,
            json={
                "source": {"longitude": 0.001, "latitude": 0},
                "destination": {"longitude": 0.003, "latitude": 0},
            },
        )
    assert response.status_code == 201
    assert response.json()["traffic_source"] == "baseline"
    assert response.json()["segment_ids"] == ["0:0"]
    assert any("unavailable" in warning for warning in response.json()["warnings"])
