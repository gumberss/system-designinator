from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import date
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import services.booking.app as booking_service
from services.activities.app import app as activities_app
from services.booking.app import Booking, BookingStep, app as booking_app
from services.flight.app import FlightOffer, FlightReservation, app as flight_app
from services.hotel.app import app as hotel_app
from services.identity.app import app as identity_app
from services.payment.app import PaymentOperation, app as payment_app


@contextmanager
def marketplace(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", "test-secret-with-enough-entropy-for-tests")
    monkeypatch.setenv("PAYMENT_SIMULATOR_MODE", "normal")
    with ExitStack() as stack:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'identity.db'}")
        identity = stack.enter_context(TestClient(identity_app))
        registered = identity.post(
            "/auth/register",
            json={
                "email": "phase3@example.test",
                "password": "a-very-long-test-password",
                "display_name": "Phase Three",
            },
        )
        assert registered.status_code == 201
        token = identity.post(
            "/auth/login",
            json={
                "email": "phase3@example.test",
                "password": "a-very-long-test-password",
            },
        ).json()["access_token"]

        clients = {}
        for name, app in (
            ("flights", flight_app),
            ("hotels", hotel_app),
            ("activities", activities_app),
            ("payment", payment_app),
            ("booking", booking_app),
        ):
            monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / f'{name}.db'}")
            clients[name] = stack.enter_context(TestClient(app))

        def call_service(
            service: str,
            method: str,
            path: str,
            *,
            token: str,
            body: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            response = clients[service].request(
                method, f"/{path}", json=body, headers=headers
            )
            if response.is_error:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.json().get("detail", "service request failed"),
                )
            return response.json()

        def search(
            service: str, path: str, params: dict[str, Any], token: str
        ) -> list[dict[str, Any]]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            query = {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in params.items()
                if value is not None
            }
            response = clients[service].get(f"/{path}", params=query, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        monkeypatch.setattr(booking_service, "_call_service", call_service)
        monkeypatch.setattr(booking_service, "_search", search)
        yield clients, token


def quote_flight(client: TestClient, token: str) -> dict[str, Any]:
    response = client.post(
        "/quotes",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "flight": {
                "offer_id": "offer-001",
                "origin": "TYO",
                "destination": "KYO",
                "passengers": 1,
            }
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_booking_saga_completes_and_start_is_idempotent(
    monkeypatch, tmp_path
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        quote = quote_flight(clients["booking"], token)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "phase3-booking-success",
        }
        payload = {"quote_id": quote["id"]}
        first = clients["booking"].post("/bookings", json=payload, headers=headers)
        replay = clients["booking"].post("/bookings", json=payload, headers=headers)

        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"]
        assert first.json()["status"] == "completed"
        assert first.json()["payment_capture_id"]
        assert first.json()["reservation_ids"]["flight"]
        with clients["flights"].app.state.session_factory() as session:
            reservation = session.scalar(select(FlightReservation))
            assert reservation is not None
            assert reservation.status == "confirmed"
        with clients["payment"].app.state.session_factory() as session:
            assert session.scalar(select(func.count(PaymentOperation.id))) == 2
        with clients["booking"].app.state.session_factory() as session:
            assert session.scalar(select(func.count(Booking.id))) == 1
            steps = list(session.scalars(select(BookingStep)))
            assert steps
            assert all(step.status == "succeeded" for step in steps)


def test_declined_payment_compensates_inventory(
    monkeypatch, tmp_path
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        quote = quote_flight(clients["booking"], token)
        monkeypatch.setenv("PAYMENT_SIMULATOR_MODE", "decline_authorization")
        response = clients["booking"].post(
            "/bookings",
            json={"quote_id": quote["id"]},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "phase3-booking-declined",
            },
        )

        assert response.status_code == 202
        assert response.json()["status"] == "failed"
        with clients["flights"].app.state.session_factory() as session:
            reservation = session.scalar(select(FlightReservation))
            offer = session.get(FlightOffer, "offer-001")
            assert reservation is not None and reservation.status == "cancelled"
            assert offer is not None and offer.seats_available == 1


def test_unknown_authorization_is_reconciled_without_duplicate_payment(
    monkeypatch, tmp_path
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        quote = quote_flight(clients["booking"], token)
        monkeypatch.setenv("PAYMENT_SIMULATOR_MODE", "unknown_authorization")
        started = clients["booking"].post(
            "/bookings",
            json={"quote_id": quote["id"]},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "phase3-booking-unknown-auth",
            },
        )
        assert started.json()["status"] == "payment_unknown"

        monkeypatch.setenv("PAYMENT_SIMULATOR_MODE", "normal")
        reconciled = clients["booking"].post(
            f"/bookings/{started.json()['id']}/reconcile",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reconciled.json()["status"] == "completed"
        with clients["payment"].app.state.session_factory() as session:
            assert session.scalar(select(func.count(PaymentOperation.id))) == 2


@pytest.mark.parametrize(
    ("operation", "persist_before_timeout"),
    [
        ("authorize", False),
        ("authorize", True),
        ("capture", False),
        ("capture", True),
    ],
)
def test_payment_timeout_preserves_holds_until_reconciled(
    monkeypatch, tmp_path, operation: str, persist_before_timeout: bool
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        quote = quote_flight(clients["booking"], token)
        original_call = booking_service._call_service
        timed_out = False

        def timeout_payment_once(service, method, path, **kwargs):
            nonlocal timed_out
            if (
                service == "payment"
                and method == "POST"
                and path == f"payments/{operation}"
                and not timed_out
            ):
                timed_out = True
                if persist_before_timeout:
                    original_call(service, method, path, **kwargs)
                raise HTTPException(status_code=504, detail="simulated timeout")
            return original_call(service, method, path, **kwargs)

        monkeypatch.setattr(booking_service, "_call_service", timeout_payment_once)
        started = clients["booking"].post(
            "/bookings",
            json={"quote_id": quote["id"]},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"phase3-payment-timeout-{operation}-{persist_before_timeout}",
            },
        )
        assert started.status_code == 202
        assert started.json()["status"] == "payment_unknown"
        with clients["flights"].app.state.session_factory() as session:
            reservation = session.scalar(select(FlightReservation))
            offer = session.get(FlightOffer, "offer-001")
            assert reservation is not None and reservation.status == "held"
            assert offer is not None and offer.seats_available == 0

        monkeypatch.setattr(booking_service, "_call_service", original_call)
        reconciled = clients["booking"].post(
            f"/bookings/{started.json()['id']}/reconcile",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "completed"
        with clients["payment"].app.state.session_factory() as session:
            assert session.scalar(select(func.count(PaymentOperation.id))) == 2


def test_booking_resumes_after_orchestrator_interruption(
    monkeypatch, tmp_path
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        quote = quote_flight(clients["booking"], token)
        original_call = booking_service._call_service
        crashed = False

        def crash_before_capture(service, method, path, **kwargs):
            nonlocal crashed
            if service == "payment" and path == "payments/capture" and not crashed:
                crashed = True
                raise RuntimeError("simulated process interruption")
            return original_call(service, method, path, **kwargs)

        monkeypatch.setattr(booking_service, "_call_service", crash_before_capture)
        headers = {
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "phase3-booking-resume",
        }
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            clients["booking"].post(
                "/bookings", json={"quote_id": quote["id"]}, headers=headers
            )
        with clients["booking"].app.state.session_factory() as session:
            booking = session.scalar(select(Booking))
            assert booking is not None
            booking_id = booking.id
            assert booking.status == "capturing_payment"
            capture_step = session.scalar(
                select(BookingStep).where(BookingStep.name == "payment:capture")
            )
            assert capture_step is not None and capture_step.status == "in_progress"

        monkeypatch.setattr(booking_service, "_call_service", original_call)
        resumed = clients["booking"].post(
            f"/bookings/{booking_id}/resume",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"


def test_failed_compensation_is_visible_and_retryable(
    monkeypatch, tmp_path
) -> None:
    with marketplace(monkeypatch, tmp_path) as (clients, token):
        response = clients["booking"].post(
            "/quotes",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "flight": {
                    "offer_id": "offer-001",
                    "origin": "TYO",
                    "destination": "KYO",
                    "passengers": 1,
                },
                "activity": {
                    "schedule_id": "schedule-001-01",
                    "city_code": "TYO",
                    "participants": 10,
                },
            },
        )
        assert response.status_code == 201
        original_call = booking_service._call_service
        release_failed = False

        def fail_first_release(service, method, path, **kwargs):
            nonlocal release_failed
            if method == "DELETE" and path.startswith("reservations/") and not release_failed:
                release_failed = True
                raise HTTPException(status_code=503, detail="simulated release outage")
            return original_call(service, method, path, **kwargs)

        monkeypatch.setattr(booking_service, "_call_service", fail_first_release)
        started = clients["booking"].post(
            "/bookings",
            json={"quote_id": response.json()["id"]},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "phase3-booking-compensation",
            },
        )
        assert started.json()["status"] == "reconciliation_required"

        monkeypatch.setattr(booking_service, "_call_service", original_call)
        resumed = clients["booking"].post(
            f"/bookings/{started.json()['id']}/resume",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resumed.json()["status"] == "failed"
        with clients["flights"].app.state.session_factory() as session:
            reservation = session.scalar(select(FlightReservation))
            offer = session.get(FlightOffer, "offer-001")
            assert reservation is not None and reservation.status == "cancelled"
            assert offer is not None and offer.seats_available == 1
