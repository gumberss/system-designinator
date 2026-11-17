from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from mapinator.api import Settings, create_app
from tests.memory import MemoryRepository
from tests.test_api import ORIGIN, sign_in
from tests.test_routing import network


def test_routes_process_pool_persistence_and_ownership() -> None:
    repo = MemoryRepository()
    repo.road_data = network(
        [(0, 0), (0.004, 0)], [(1, 2, {"name": "Fixture Street", "oneway": "yes"})]
    )
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as client:
        headers = sign_in(client)
        payload = {
            "source": {"latitude": 0, "longitude": 0.001},
            "destination": {"latitude": 0, "longitude": 0.003},
        }
        result = client.post("/api/v1/routes", json=payload, headers=headers)
        assert result.status_code == 201, result.text
        route = result.json()
        assert route["distance_m"] > 200 and route["eta_s"] > 0
        assert route["instructions"][-1]["action"] == "arrive"
        assert client.get(f"/api/v1/routes/{route['route_id']}").json() == route
        reversed_payload = {"source": payload["destination"], "destination": payload["source"]}
        failed = client.post("/api/v1/routes", json=reversed_payload, headers=headers)
        assert failed.status_code == 422 and failed.json()["code"] == "route_not_found"
        # A worker-reported domain error must not poison the pool.
        assert client.post("/api/v1/routes", json=payload, headers=headers).status_code == 201
        assert client.post("/api/v1/routes", json=payload).status_code == 403
        with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as other:
            sign_in(other)
            assert other.get(f"/api/v1/routes/{route['route_id']}").status_code == 404
        saved_owner, saved, _ = next(iter(repo.routes.values()))
        first_id = next(iter(repo.routes))
        repo.routes[first_id] = (saved_owner, saved, datetime.now(UTC) - timedelta(seconds=1))
        assert client.get(f"/api/v1/routes/{first_id}").status_code == 404


def test_absent_graph_validation_and_ambiguous_geocoding() -> None:
    with TestClient(create_app(MemoryRepository(), Settings(allowed_origins=ORIGIN))) as client:
        headers = sign_in(client)
        payload = {
            "source": {"latitude": 0, "longitude": 0},
            "destination": {"latitude": 0, "longitude": 0.001},
        }
        assert client.post("/api/v1/routes", json=payload, headers=headers).status_code == 503
        assert (
            client.post(
                "/api/v1/routes", json={**payload, "mode": "transit"}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/routes", json={**payload, "alternatives": 3}, headers=headers
            ).status_code
            == 422
        )
