from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from mapinator.api import Settings, create_app
from mapinator.domain import SearchHit
from tests.memory import MemoryRepository

ORIGIN = "http://testserver"


@pytest.fixture
def repo() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def client(repo: MemoryRepository) -> Iterator[TestClient]:
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as value:
        yield value


def sign_in(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/session", headers={"origin": ORIGIN})
    assert response.status_code in (200, 201)
    return {"origin": ORIGIN, "x-csrf-token": response.json()["csrf_token"]}


def enable_tracking(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/api/v1/location/tracking", json={"tracking_consent": True}, headers=headers
    )
    assert response.status_code == 201
    return str(response.json()["session_id"])


def observation(session: str, sequence: int = 1) -> dict[str, object]:
    return {
        "event_id": str(uuid4()),
        "session_id": session,
        "sequence": sequence,
        "timestamp": datetime.now(UTC).isoformat(),
        "latitude": -23.56,
        "longitude": -46.65,
        "accuracy_m": 10,
        "heading_deg": None,
        "speed_mps": None,
    }


def test_health_static_openapi_capabilities(client: TestClient) -> None:
    assert client.get("/health/ready").json()["phase"] == "7"
    assert "Mapinator" in client.get("/").text
    assert client.get("/app.js").status_code == 200
    assert client.get("/openapi.json").json()["info"]["version"] == "0.7.0"
    assert client.get("/api/v1/config").json()["capabilities"]["routing"] is True
    assert "mapinator_requests_total" in client.get("/metrics").text
    assert client.get("/api/v1/geocoding/search?q=Fixture").status_code == 503


def test_cookie_authentication_origin_csrf(client: TestClient) -> None:
    assert client.get("/api/v1/location/current").status_code == 401
    rejected = client.post("/api/v1/auth/session", headers={"origin": "https://evil.invalid"})
    assert rejected.status_code == 403
    assert rejected.headers["content-type"].startswith("application/problem+json")
    accepted = client.post("/api/v1/auth/session", headers={"origin": ORIGIN})
    cookie = accepted.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert (
        client.post(
            "/api/v1/location/tracking", json={"tracking_consent": True}, headers={"origin": ORIGIN}
        ).status_code
        == 403
    )
    assert accepted.headers["x-request-id"]


def test_explicit_consent_and_missing_session(client: TestClient) -> None:
    headers = sign_in(client)
    assert (
        client.post(
            "/api/v1/location/tracking", json={"tracking_consent": False}, headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post("/api/v1/location", json=observation(str(uuid4())), headers=headers).status_code
        == 403
    )


def test_location_deduplication_ordering_and_revocation(
    client: TestClient, repo: MemoryRepository
) -> None:
    headers = sign_in(client)
    session = enable_tracking(client, headers)
    event = observation(session)
    assert client.post("/api/v1/location", json=event, headers=headers).status_code == 202
    assert client.post("/api/v1/location", json=event, headers=headers).json()["deduplicated"]
    modified = {**event, "latitude": -23.57}
    assert client.post("/api/v1/location", json=modified, headers=headers).status_code == 409
    current = client.get("/api/v1/location/current").json()
    assert current["latitude"] == -23.56 and current["freshness_s"] >= 0
    stale = observation(session, sequence=0)
    assert client.post("/api/v1/location", json=stale, headers=headers).status_code == 409
    repo.cleanup(datetime.now(UTC) + timedelta(minutes=6))
    assert client.get("/api/v1/location/current").status_code == 404
    assert client.delete("/api/v1/location/current", headers=headers).status_code == 200
    assert (
        client.post("/api/v1/location", json=observation(session, 2), headers=headers).status_code
        == 403
    )
    assert client.delete("/api/v1/location/current", headers=headers).status_code == 200


def test_reactivation_rotates_session_and_resets_sequence(client: TestClient) -> None:
    headers = sign_in(client)
    old = enable_tracking(client, headers)
    assert (
        client.post("/api/v1/location", json=observation(old, 100), headers=headers).status_code
        == 202
    )
    new = enable_tracking(client, headers)
    assert new != old
    assert client.get("/api/v1/location/current").status_code == 404
    assert (
        client.post("/api/v1/location", json=observation(new, 1), headers=headers).status_code
        == 202
    )
    assert (
        client.post("/api/v1/location", json=observation(old, 101), headers=headers).status_code
        == 403
    )


def test_cross_owner_location_isolation(client: TestClient, repo: MemoryRepository) -> None:
    first = sign_in(client)
    session = enable_tracking(client, first)
    assert (
        client.post("/api/v1/location", json=observation(session), headers=first).status_code == 202
    )
    with TestClient(create_app(repo, Settings(allowed_origins=ORIGIN))) as other:
        second = sign_in(other)
        assert other.get("/api/v1/location/current").status_code == 404
        assert (
            other.post("/api/v1/location", json=observation(session, 2), headers=second).status_code
            == 403
        )
    assert client.get("/api/v1/location/current").status_code == 200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", 91),
        ("longitude", -181),
        ("accuracy_m", -1),
        ("heading_deg", 360),
        ("speed_mps", -1),
        ("sequence", -1),
        ("timestamp", "2020-01-01T00:00:00Z"),
        ("timestamp", "2026-11-15T23:00:00"),
        ("unknown_field", "rejected"),
    ],
)
def test_schema_rejects_invalid_observations(client: TestClient, field: str, value: object) -> None:
    headers = sign_in(client)
    event = observation(enable_tracking(client, headers))
    event[field] = value
    assert client.post("/api/v1/location", json=event, headers=headers).status_code == 422


def test_search_pagination_bounds_and_unknown_dataset(
    client: TestClient, repo: MemoryRepository
) -> None:
    now = datetime.now(UTC)
    dataset_id = uuid4()
    repo.hits = [
        SearchHit(
            f"node/{i}",
            f"Fixture Cafe {i}",
            -23.56,
            -46.65,
            "cafe",
            {},
            "osm_node",
            dataset_id,
            now,
        )
        for i in range(3)
    ]
    first = client.get("/api/v1/geocoding/search", params={"q": "cafe", "limit": 2}).json()
    assert len(first["items"]) == 2 and first["next_cursor"] == 2
    second = client.get(
        "/api/v1/geocoding/search", params={"q": "cafe", "limit": 2, "cursor": 2}
    ).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    outside = client.get("/api/v1/geocoding/search", params={"q": "cafe", "bbox": "0,0,1,1"}).json()
    assert outside["items"] == []
    assert client.get("/api/v1/geocoding/search?q=cafe&bbox=nan,0,1,1").status_code == 422
    assert client.get("/api/v1/geocoding/search?q=cafe&bbox=1,0,0,1").status_code == 422
    assert client.get("/api/v1/geocoding/search?q=%20%20").status_code == 422
    assert client.get("/api/v1/datasets/current").status_code == 404


def test_db_failure_is_explicit_and_not_parameter_leak() -> None:
    class FailingRepository(MemoryRepository):
        def search(
            self,
            query: str,
            bounds: tuple[float, float, float, float] | None,
            limit: int,
            offset: int,
        ) -> list[SearchHit]:
            raise OperationalError("query", {"secret": "private-coordinate"}, Exception("failed"))

    with TestClient(create_app(FailingRepository(), Settings())) as client:
        response = client.get("/api/v1/geocoding/search?q=cafe")
    assert response.status_code == 503
    assert "private-coordinate" not in response.text
