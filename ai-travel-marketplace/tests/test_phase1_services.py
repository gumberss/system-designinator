from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text

import services.activities.app as activities_service
import services.flight.app as flight_service
import services.hotel.app as hotel_service
from services.activities.app import app as activities_app
from services.activities.app import Activity
from services.destination.app import app as destination_app
from services.flight.app import app as flight_app
from services.flight.app import Airline, FlightOffer
from services.hotel.app import app as hotel_app
from services.hotel.app import Hotel
from services.identity.app import app as identity_app


def use_database(monkeypatch, tmp_path, service: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'{service}.db'}")
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-enough-entropy-for-tests")


@pytest.mark.parametrize(
    ("migrate", "table_name"),
    [
        (flight_service._migrate, "flight_reservations"),
        (hotel_service._migrate, "hotel_reservations"),
        (activities_service._migrate, "activity_reservations"),
    ],
)
def test_existing_reservation_schemas_gain_expiry_column(
    tmp_path, migrate, table_name
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(f"CREATE TABLE {table_name} (id VARCHAR(36) PRIMARY KEY)")
        )

    migrate(engine)

    columns = {column["name"] for column in inspect(engine).get_columns(table_name)}
    assert "expires_at" in columns
    engine.dispose()


def register_and_login(monkeypatch, tmp_path) -> str:
    use_database(monkeypatch, tmp_path, "identity")
    with TestClient(identity_app) as client:
        registered = client.post(
            "/auth/register",
            json={
                "email": "traveler@example.test",
                "password": "a-very-long-test-password",
                "display_name": "Test Traveler",
            },
        )
        assert registered.status_code == 201
        response = client.post(
            "/auth/login",
            json={
                "email": "traveler@example.test",
                "password": "a-very-long-test-password",
            },
        )
        assert response.status_code == 200
        return response.json()["access_token"]


def test_destination_seed_has_required_relationship_counts(monkeypatch, tmp_path) -> None:
    use_database(monkeypatch, tmp_path, "destination")
    with TestClient(destination_app) as client:
        assert len(client.get("/countries").json()) == 10
        assert len(client.get("/cities").json()) == 30
        attractions = client.get("/attractions")
        assert attractions.status_code == 200
        assert len(attractions.json()) == 100
        assert all(item["city_code"] for item in attractions.json())


def test_flight_reservation_is_authenticated_and_idempotent(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    use_database(monkeypatch, tmp_path, "flight")
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "flight-request-0001",
    }
    payload = {"offer_id": "offer-001", "passengers": 1}
    with TestClient(flight_app) as client:
        with client.app.state.session_factory() as session:
            assert session.scalar(select(func.count(Airline.code))) == 30
            assert session.scalar(select(func.count(FlightOffer.id))) == 100
        first = client.post("/reservations", json=payload, headers=headers)
        replay = client.post("/reservations", json=payload, headers=headers)
        assert first.status_code == 201
        assert replay.status_code == 201
        assert first.json()["status"] == "held"
        assert first.json()["expires_at"] is not None
        assert first.json()["id"] == replay.json()["id"]

        changed_payload = {"offer_id": "offer-001", "passengers": 2}
        conflict = client.post(
            "/reservations", json=changed_payload, headers=headers
        )
        assert conflict.status_code == 409


def test_holds_expire_and_restore_inventory_for_all_services(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    clock = [datetime.now(timezone.utc).replace(tzinfo=None)]
    monkeypatch.setattr(flight_service, "utc_now", lambda: clock[0])
    use_database(monkeypatch, tmp_path, "flight")
    flight_headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "expiring-flight-hold",
    }
    flight_payload = {"offer_id": "offer-001", "passengers": 1}
    with TestClient(flight_app) as client:
        held = client.post(
            "/reservations", json=flight_payload, headers=flight_headers
        )
        assert held.status_code == 201
        assert held.json()["status"] == "held"
        assert held.json()["expires_at"] is not None

        clock[0] += timedelta(minutes=16)
        replay = client.post(
            "/reservations", json=flight_payload, headers=flight_headers
        )
        assert replay.status_code == 201
        assert replay.json()["id"] == held.json()["id"]
        assert replay.json()["status"] == "expired"
        assert client.post(
            f"/reservations/{held.json()['id']}/confirm",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 409
        with client.app.state.session_factory() as session:
            offer = session.get(FlightOffer, "offer-001")
            assert offer is not None
            assert offer.seats_available == 1

    clock[0] = datetime.now(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(hotel_service, "utc_now", lambda: clock[0])
    use_database(monkeypatch, tmp_path, "hotel")
    check_in = date.today() + timedelta(days=8)
    hotel_payload = {
        "hotel_id": "hotel-001",
        "room_type_id": "hotel-001-room-1",
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=1)).isoformat(),
        "units": 1,
        "guests": 2,
    }
    hotel_headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "expiring-hotel-hold",
    }
    with TestClient(hotel_app) as client:
        held = client.post(
            "/reservations", json=hotel_payload, headers=hotel_headers
        )
        assert held.status_code == 201
        clock[0] += timedelta(minutes=16)
        replay = client.post(
            "/reservations", json=hotel_payload, headers=hotel_headers
        )
        assert replay.json()["status"] == "expired"
        availability = client.get(
            "/hotels/search",
            params={
                "city_code": "TYO",
                "check_in": hotel_payload["check_in"],
                "check_out": hotel_payload["check_out"],
                "guests": 2,
            },
        )
        room = next(
            result
            for result in availability.json()
            if result["room_type_id"] == "hotel-001-room-1"
        )
        assert room["available_units"] == 3 + (8 % 8)

    clock[0] = datetime.now(timezone.utc).replace(tzinfo=None)
    monkeypatch.setattr(activities_service, "utc_now", lambda: clock[0])
    use_database(monkeypatch, tmp_path, "activities")
    activity_payload = {"schedule_id": "schedule-001-01", "participants": 1}
    activity_headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "expiring-activity-hold",
    }
    with TestClient(activities_app) as client:
        held = client.post(
            "/reservations", json=activity_payload, headers=activity_headers
        )
        assert held.status_code == 201
        clock[0] += timedelta(minutes=16)
        replay = client.post(
            "/reservations", json=activity_payload, headers=activity_headers
        )
        assert replay.json()["status"] == "expired"
        availability = client.get(
            "/activities/search",
            params={"city_code": "TYO"},
        )
        schedule = next(
            result
            for result in availability.json()
            if result["schedule_id"] == activity_payload["schedule_id"]
        )
        assert schedule["remaining_capacity"] == 9


def test_cancellation_releases_inventory_once_and_confirmation_is_idempotent(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    use_database(monkeypatch, tmp_path, "flight")
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "cancel-flight-hold",
    }
    with TestClient(flight_app) as client:
        held = client.post(
            "/reservations",
            json={"offer_id": "offer-001", "passengers": 1},
            headers=headers,
        )
        reservation_id = held.json()["id"]
        confirmed = client.post(
            f"/reservations/{reservation_id}/confirm",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "confirmed"
        repeated_confirmation = client.post(
            f"/reservations/{reservation_id}/confirm",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert repeated_confirmation.json()["status"] == "confirmed"

        cancelled = client.delete(
            f"/reservations/{reservation_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        repeated_cancellation = client.delete(
            f"/reservations/{reservation_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cancelled.json()["status"] == "cancelled"
        assert repeated_cancellation.json()["status"] == "cancelled"
        with client.app.state.session_factory() as session:
            offer = session.get(FlightOffer, "offer-001")
            assert offer is not None
            assert offer.seats_available == 1

    check_in = date.today() + timedelta(days=8)
    hotel_payload = {
        "hotel_id": "hotel-001",
        "room_type_id": "hotel-001-room-1",
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=1)).isoformat(),
        "units": 1,
        "guests": 2,
    }
    use_database(monkeypatch, tmp_path, "hotel")
    with TestClient(hotel_app) as client:
        held = client.post(
            "/reservations",
            json=hotel_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "cancel-hotel-hold",
            },
        )
        reservation_id = held.json()["id"]
        for _ in range(2):
            cancelled = client.delete(
                f"/reservations/{reservation_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert cancelled.json()["status"] == "cancelled"
        availability = client.get(
            "/hotels/search",
            params={
                "city_code": "TYO",
                "check_in": hotel_payload["check_in"],
                "check_out": hotel_payload["check_out"],
                "guests": 2,
            },
        )
        room = next(
            result
            for result in availability.json()
            if result["room_type_id"] == "hotel-001-room-1"
        )
        assert room["available_units"] == 3

    use_database(monkeypatch, tmp_path, "activities")
    with TestClient(activities_app) as client:
        held = client.post(
            "/reservations",
            json={"schedule_id": "schedule-001-01", "participants": 1},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "cancel-activity-hold",
            },
        )
        reservation_id = held.json()["id"]
        for _ in range(2):
            cancelled = client.delete(
                f"/reservations/{reservation_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert cancelled.json()["status"] == "cancelled"
        availability = client.get(
            "/activities/search",
            params={"city_code": "TYO"},
        )
        schedule = next(
            result
            for result in availability.json()
            if result["schedule_id"] == "schedule-001-01"
        )
        assert schedule["remaining_capacity"] == 9


def test_hotel_and_activity_capacity_is_not_oversold_concurrently(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    check_in = date.today() + timedelta(days=8)
    hotel_payload = {
        "hotel_id": "hotel-001",
        "room_type_id": "hotel-001-room-1",
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=1)).isoformat(),
        "units": 3,
        "guests": 2,
    }
    use_database(monkeypatch, tmp_path, "hotel")
    with TestClient(hotel_app) as client:
        def reserve_hotel(index: int) -> int:
            response = client.post(
                "/reservations",
                json=hotel_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": f"hotel-capacity-{index:04}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            hotel_statuses = sorted(executor.map(reserve_hotel, (1, 2)))
        assert hotel_statuses == [201, 409]

    use_database(monkeypatch, tmp_path, "activities")
    with TestClient(activities_app) as client:
        def reserve_activity(index: int) -> int:
            response = client.post(
                "/reservations",
                json={"schedule_id": "schedule-001-01", "participants": 9},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": f"activity-capacity-{index:04}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            activity_statuses = sorted(executor.map(reserve_activity, (1, 2)))
        assert activity_statuses == [201, 409]


def test_flight_inventory_is_not_oversold_by_concurrent_requests(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    use_database(monkeypatch, tmp_path, "flight")
    with TestClient(flight_app) as client:
        def reserve(index: int) -> int:
            response = client.post(
                "/reservations",
                json={"offer_id": "offer-001", "passengers": 1},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": f"concurrent-flight-{index:04}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = sorted(executor.map(reserve, (1, 2)))
        assert statuses == [201, 409]


def test_hotel_and_activity_reservations_replay_idempotently(
    monkeypatch, tmp_path
) -> None:
    token = register_and_login(monkeypatch, tmp_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "hotel-request-0001",
    }
    check_in = date.today() + timedelta(days=8)
    check_out = check_in + timedelta(days=2)
    hotel_payload = {
        "hotel_id": "hotel-001",
        "room_type_id": "hotel-001-room-1",
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "units": 1,
        "guests": 2,
    }
    use_database(monkeypatch, tmp_path, "hotel")
    with TestClient(hotel_app) as client:
        with client.app.state.session_factory() as session:
            assert session.scalar(select(func.count(Hotel.id))) == 50
        first = client.post("/reservations", json=hotel_payload, headers=headers)
        replay = client.post("/reservations", json=hotel_payload, headers=headers)
        assert first.status_code == replay.status_code == 201
        assert first.json()["status"] == "held"
        assert first.json()["expires_at"] is not None
        assert first.json()["id"] == replay.json()["id"]

    activity_headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "activity-request-0001",
    }
    activity_payload = {"schedule_id": "schedule-001-01", "participants": 2}
    use_database(monkeypatch, tmp_path, "activities")
    with TestClient(activities_app) as client:
        with client.app.state.session_factory() as session:
            assert session.scalar(select(func.count(Activity.id))) == 100
        first = client.post(
            "/reservations", json=activity_payload, headers=activity_headers
        )
        replay = client.post(
            "/reservations", json=activity_payload, headers=activity_headers
        )
        assert first.status_code == replay.status_code == 201
        assert first.json()["status"] == "held"
        assert first.json()["expires_at"] is not None
        assert first.json()["id"] == replay.json()["id"]


def test_hotel_and_activity_searches_return_seeded_inventory(
    monkeypatch, tmp_path
) -> None:
    use_database(monkeypatch, tmp_path, "hotel")
    check_in = date.today() + timedelta(days=5)
    check_out = check_in + timedelta(days=2)
    with TestClient(hotel_app) as client:
        hotels = client.get(
            "/hotels/search",
            params={
                "city_code": "TYO",
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "guests": 2,
            },
        )
        assert hotels.status_code == 200
        assert hotels.json()

    use_database(monkeypatch, tmp_path, "activities")
    with TestClient(activities_app) as client:
        activities = client.get(
            "/activities/search",
            params={"city_code": "TYO"},
        )
        assert activities.status_code == 200
        assert activities.json()
