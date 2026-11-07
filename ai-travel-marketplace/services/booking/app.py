from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, ForeignKey, JSON, Numeric, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.common.booking import utc_now
from packages.common.database import service_lifespan, session_dependency
from packages.common.events import emit_event
from packages.common.security import current_user_id

QUOTE_TTL = timedelta(minutes=10)
SERVICE_ENV = {
    "flights": "FLIGHT_URL",
    "hotels": "HOTEL_URL",
    "activities": "ACTIVITIES_URL",
}
SERVICE_NAMES = {
    "flight": "flights",
    "hotel": "hotels",
    "activity": "activities",
}


class BookingBase(DeclarativeBase):
    pass


class Quote(BookingBase):
    __tablename__ = "booking_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    selection: Mapped[dict[str, Any]] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default="open")


class Booking(BookingBase):
    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_booking_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quote_id: Mapped[str] = mapped_column(ForeignKey("booking_quotes.id"))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="starting")
    selection: Mapped[dict[str, Any]] = mapped_column(JSON)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payment_authorization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payment_capture_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class BookingStep(BookingBase):
    __tablename__ = "booking_steps"
    __table_args__ = (
        UniqueConstraint("booking_id", "name", name="uq_booking_step"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    operation_key: Mapped[str] = mapped_column(String(180), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class FlightSelection(BaseModel):
    offer_id: str = Field(min_length=1, max_length=36)
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    departure_date: date | None = None
    passengers: int = Field(ge=1, le=9)


class HotelSelection(BaseModel):
    hotel_id: str = Field(min_length=1, max_length=36)
    room_type_id: str = Field(min_length=1, max_length=36)
    city_code: str = Field(min_length=3, max_length=3)
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=5)
    guests: int = Field(default=1, ge=1, le=8)


class ActivitySelection(BaseModel):
    schedule_id: str = Field(min_length=1, max_length=36)
    city_code: str = Field(min_length=3, max_length=3)
    activity_date: date | None = None
    participants: int = Field(ge=1, le=12)


class QuoteRequest(BaseModel):
    flight: FlightSelection | None = None
    hotel: HotelSelection | None = None
    activity: ActivitySelection | None = None


class StartBookingRequest(BaseModel):
    quote_id: str = Field(min_length=1, max_length=36)


class QuoteView(BaseModel):
    id: str
    selection: dict[str, Any]
    fingerprint: str
    total_amount: Decimal
    currency: str
    expires_at: datetime
    status: str


class BookingView(BaseModel):
    id: str
    quote_id: str
    status: str
    total_amount: Decimal
    currency: str
    failure_reason: str | None
    version: int
    reservation_ids: dict[str, str]
    payment_authorization_id: str | None
    payment_capture_id: str | None


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


app = FastAPI(
    title="Booking Orchestrator",
    version="0.1.0",
    lifespan=service_lifespan(
        base=BookingBase,
        seed=lambda session: None,
        default_database_url="sqlite:///./booking.db",
        events_producer="booking",
    ),
)


def _call_service(
    service: str,
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    base_url = os.getenv(SERVICE_ENV.get(service, "PAYMENT_URL"))
    if not base_url:
        raise HTTPException(
            status_code=503, detail=f"{service} service URL is not configured"
        )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
            response = client.request(
                method,
                f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail=f"{service} service timed out") from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503, detail=f"{service} service is unavailable"
        ) from error
    if response.is_error:
        detail = response.json().get("detail", f"{service} request failed")
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


def _service_base(service: str) -> str:
    environment_key = SERVICE_ENV[service]
    value = os.getenv(environment_key)
    if not value:
        raise HTTPException(
            status_code=503, detail=f"{service} service URL is not configured"
        )
    return value.rstrip("/")


def _search(
    service: str, path: str, params: dict[str, Any], token: str
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
            response = client.get(
                f"{_service_base(service)}/{path}",
                params={
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in params.items()
                    if value is not None
                },
                headers=headers,
            )
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503, detail=f"{service} service is unavailable"
        ) from error
    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    result = response.json()
    if not isinstance(result, list):
        raise HTTPException(status_code=502, detail=f"{service} returned an invalid search response")
    return result


def _quote_selection(request: QuoteRequest, token: str) -> dict[str, Any]:
    selection: dict[str, Any] = {}
    prices: list[tuple[Decimal, str]] = []
    if request.flight:
        item = request.flight
        matches = _search(
            "flights",
            "flights/search",
            {
                "origin": item.origin.upper(),
                "destination": item.destination.upper(),
                "departure_date": item.departure_date,
                "passengers": item.passengers,
            },
            token,
        )
        found = next((row for row in matches if row["id"] == item.offer_id), None)
        if found is None:
            raise HTTPException(status_code=409, detail="Selected flight is no longer available")
        price = Decimal(str(found["price"])) * item.passengers
        selection["flight"] = {
            "reservation": {"offer_id": item.offer_id, "passengers": item.passengers},
            "price": str(price),
            "currency": found["currency"],
        }
        prices.append((price, found["currency"]))
    if request.hotel:
        item = request.hotel
        matches = _search(
            "hotels",
            "hotels/search",
            {
                "city_code": item.city_code.upper(),
                "check_in": item.check_in,
                "check_out": item.check_out,
                "guests": item.guests,
            },
            token,
        )
        found = next(
            (
                row
                for row in matches
                if row["hotel_id"] == item.hotel_id
                and row["room_type_id"] == item.room_type_id
            ),
            None,
        )
        if found is None:
            raise HTTPException(status_code=409, detail="Selected hotel is no longer available")
        nights = (item.check_out - item.check_in).days
        if nights <= 0 or nights > 30:
            raise HTTPException(status_code=422, detail="Invalid hotel stay")
        price = Decimal(str(found["price_per_night"])) * item.units * nights
        selection["hotel"] = {
            "reservation": item.model_dump(mode="json"),
            "price": str(price),
            "currency": found["currency"],
        }
        prices.append((price, found["currency"]))
    if request.activity:
        item = request.activity
        matches = _search(
            "activities",
            "activities/search",
            {"city_code": item.city_code.upper(), "activity_date": item.activity_date},
            token,
        )
        found = next(
            (row for row in matches if row["schedule_id"] == item.schedule_id), None
        )
        if found is None:
            raise HTTPException(
                status_code=409, detail="Selected activity is no longer available"
            )
        price = Decimal(str(found["price_per_person"])) * item.participants
        selection["activity"] = {
            "reservation": {
                "schedule_id": item.schedule_id,
                "participants": item.participants,
            },
            "price": str(price),
            "currency": found["currency"],
        }
        prices.append((price, found["currency"]))
    if not prices:
        raise HTTPException(status_code=422, detail="At least one booking item is required")
    currencies = {currency.upper() for _, currency in prices}
    if len(currencies) != 1:
        raise HTTPException(status_code=422, detail="All booking items must use one currency")
    return {
        "selection": selection,
        "total_amount": str(sum((price for price, _ in prices), Decimal("0.00"))),
        "currency": currencies.pop(),
    }


def _booking_view(session: Session, booking: Booking) -> dict[str, Any]:
    steps = list(
        session.scalars(
            select(BookingStep).where(
                BookingStep.booking_id == booking.id,
                BookingStep.name.like("hold:%"),
            )
        )
    )
    return {
        "id": booking.id,
        "quote_id": booking.quote_id,
        "status": booking.status,
        "total_amount": booking.total_amount,
        "currency": booking.currency,
        "failure_reason": booking.failure_reason,
        "version": booking.version,
        "reservation_ids": {
            step.name.split(":", maxsplit=1)[1]: step.result["id"]
            for step in steps
            if step.result and step.result.get("id")
        },
        "payment_authorization_id": booking.payment_authorization_id,
        "payment_capture_id": booking.payment_capture_id,
    }


def _persist(session: Session, booking_id: str, **values: Any) -> None:
    booking = session.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    for key, value in values.items():
        setattr(booking, key, value)
    booking.version += 1
    booking.updated_at = utc_now()
    emit_event(
        session,
        producer="booking",
        event_type="BookingStateChanged",
        aggregate_type="booking",
        aggregate_id=booking.id,
        aggregate_version=booking.version,
        payload={"booking_id": booking.id, "status": booking.status},
    )
    session.commit()


def _step(session: Session, booking_id: str, name: str) -> BookingStep:
    return session.scalar(
        select(BookingStep).where(
            BookingStep.booking_id == booking_id, BookingStep.name == name
        )
    )


def _step_start(session: Session, step_id: str) -> None:
    step = session.get(BookingStep, step_id)
    step.status = "in_progress"
    step.error = None
    session.commit()


def _step_finish(
    session: Session,
    step_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    step = session.get(BookingStep, step_id)
    step.status = status
    step.result = result
    step.error = error
    session.commit()


def _ensure_step(session: Session, booking: Booking, name: str) -> BookingStep:
    step = _step(session, booking.id, name)
    if step is None:
        step = BookingStep(
            id=str(uuid.uuid4()),
            booking_id=booking.id,
            name=name,
            operation_key=f"booking:{booking.id}:{name}",
            status="pending",
        )
        session.add(step)
        session.commit()
        session.refresh(step)
    return step


def _do_step(
    session: Session,
    booking: Booking,
    name: str,
    call: Any,
) -> dict[str, Any]:
    step = _ensure_step(session, booking, name)
    if step.status == "succeeded":
        return step.result or {}
    _step_start(session, step.id)
    try:
        result = call(step.operation_key)
    except HTTPException as error:
        _step_finish(session, step.id, status="failed", error=str(error.detail))
        raise
    _step_finish(session, step.id, status="succeeded", result=result)
    return result


def _record_unknown_payment(
    session: Session, booking: Booking, operation: str, error: HTTPException
) -> None:
    step = _step(session, booking.id, f"payment:{operation}")
    if step is not None:
        _step_finish(
            session,
            step.id,
            status="unknown",
            error=f"Payment outcome is unknown: {error.detail}",
        )
    _persist(
        session,
        booking.id,
        status="payment_unknown",
        failure_reason=f"Payment {operation} outcome is unknown",
    )


def _payment_error_is_ambiguous(error: HTTPException) -> bool:
    return error.status_code >= 500


def _compensate(
    session: Session, booking: Booking, token: str, reason: str
) -> None:
    _persist(session, booking.id, status="compensating", failure_reason=reason)
    failures: list[str] = []
    for kind in ("activity", "hotel", "flight"):
        hold = _step(session, booking.id, f"hold:{kind}")
        if hold is None or hold.status != "succeeded" or not hold.result:
            continue
        release_name = f"release:{kind}"
        release = _ensure_step(session, booking, release_name)
        if release.status == "succeeded":
            continue
        _step_start(session, release.id)
        try:
            result = _call_service(
                SERVICE_NAMES[kind],
                "DELETE",
                f"reservations/{hold.result['id']}",
                token=token,
                idempotency_key=release.operation_key,
            )
            _step_finish(session, release.id, status="succeeded", result=result)
        except HTTPException as error:
            _step_finish(session, release.id, status="failed", error=str(error.detail))
            failures.append(f"{kind} release: {error.detail}")

    capture = _step(session, booking.id, "payment:capture")
    authorization = _step(session, booking.id, "payment:authorize")
    if capture and capture.status == "succeeded":
        operation = "refund"
    elif (
        authorization
        and authorization.status == "succeeded"
        and authorization.result
        and authorization.result.get("status") == "authorized"
    ):
        operation = "void"
    else:
        operation = ""
    if operation:
        payment_step = _ensure_step(session, booking, f"payment:{operation}")
        if payment_step.status != "succeeded":
            _step_start(session, payment_step.id)
            try:
                payment = _call_service(
                    "payment",
                    "POST",
                    f"payments/{operation}",
                    token=token,
                    body={
                        "booking_id": booking.id,
                        "amount": str(booking.total_amount),
                        "currency": booking.currency,
                    },
                    idempotency_key=payment_step.operation_key,
                )
                if payment["status"] not in {"voided", "refunded"}:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Payment {operation} did not complete: {payment['status']}",
                    )
                _step_finish(
                    session, payment_step.id, status="succeeded", result=payment
                )
            except HTTPException as error:
                _step_finish(
                    session, payment_step.id, status="failed", error=str(error.detail)
                )
                failures.append(f"payment {operation}: {error.detail}")
    _persist(
        session,
        booking.id,
        status="reconciliation_required" if failures else "failed",
        failure_reason="; ".join([reason, *failures])[:500],
    )


def _run_saga(session: Session, booking: Booking, token: str) -> Booking:
    if booking.status in {"completed", "failed", "cancelled"}:
        return booking
    if booking.status == "payment_unknown":
        return booking
    _persist(session, booking.id, status="reserving")
    session.refresh(booking)
    try:
        for kind in ("flight", "hotel", "activity"):
            item = booking.selection.get(kind)
            if not item:
                continue
            plural = SERVICE_NAMES[kind]
            try:
                held = _do_step(
                    session,
                    booking,
                    f"hold:{kind}",
                    lambda key, plural=plural, item=item: _call_service(
                        plural,
                        "POST",
                        "reservations",
                        token=token,
                        body=item["reservation"],
                        idempotency_key=key,
                    ),
                )
            except HTTPException as error:
                _compensate(session, booking, token, f"{kind} hold failed: {error.detail}")
                session.refresh(booking)
                return booking
            if Decimal(str(held["total_price"])) != Decimal(item["price"]):
                _compensate(session, booking, token, f"{kind} price changed after quote")
                session.refresh(booking)
                return booking

        _persist(session, booking.id, status="authorizing_payment")
        session.refresh(booking)
        try:
            authorization = _do_step(
                session,
                booking,
                "payment:authorize",
                lambda key: _call_service(
                    "payment",
                    "POST",
                    "payments/authorize",
                    token=token,
                    body={
                        "booking_id": booking.id,
                        "amount": str(booking.total_amount),
                        "currency": booking.currency,
                    },
                    idempotency_key=key,
                ),
            )
        except HTTPException as error:
            if not _payment_error_is_ambiguous(error):
                raise
            _record_unknown_payment(session, booking, "authorize", error)
            session.refresh(booking)
            return booking
        if authorization["status"] == "unknown":
            _persist(
                session,
                booking.id,
                status="payment_unknown",
                payment_authorization_id=authorization["id"],
            )
            session.refresh(booking)
            return booking
        if authorization["status"] != "authorized":
            _compensate(
                session, booking, token, f"Payment authorization {authorization['status']}"
            )
            session.refresh(booking)
            return booking
        _persist(
            session,
            booking.id,
            payment_authorization_id=authorization["id"],
            status="capturing_payment",
        )
        session.refresh(booking)

        try:
            capture = _do_step(
                session,
                booking,
                "payment:capture",
                lambda key: _call_service(
                    "payment",
                    "POST",
                    "payments/capture",
                    token=token,
                    body={
                        "booking_id": booking.id,
                        "amount": str(booking.total_amount),
                        "currency": booking.currency,
                    },
                    idempotency_key=key,
                ),
            )
        except HTTPException as error:
            if not _payment_error_is_ambiguous(error):
                raise
            _record_unknown_payment(session, booking, "capture", error)
            session.refresh(booking)
            return booking
        if capture["status"] == "unknown":
            _persist(
                session,
                booking.id,
                status="payment_unknown",
                payment_capture_id=capture["id"],
            )
            session.refresh(booking)
            return booking
        if capture["status"] != "captured":
            _compensate(session, booking, token, f"Payment capture {capture['status']}")
            session.refresh(booking)
            return booking
        _persist(
            session,
            booking.id,
            payment_capture_id=capture["id"],
            status="confirming_reservations",
        )
        session.refresh(booking)
        for kind in ("flight", "hotel", "activity"):
            hold = _step(session, booking.id, f"hold:{kind}")
            if hold is None or hold.status != "succeeded" or not hold.result:
                continue
            _do_step(
                session,
                booking,
                f"confirm:{kind}",
                lambda key, kind=kind, hold=hold: _call_service(
                    SERVICE_NAMES[kind],
                    "POST",
                    f"reservations/{hold.result['id']}/confirm",
                    token=token,
                    idempotency_key=key,
                ),
            )
        _persist(session, booking.id, status="completed", failure_reason=None)
        session.refresh(booking)
        quote = session.get(Quote, booking.quote_id)
        if quote is not None:
            quote.status = "consumed"
            session.commit()
        return booking
    except HTTPException as error:
        if booking.status == "payment_unknown":
            return booking
        _compensate(session, booking, token, f"Saga step failed: {error.detail}")
        session.refresh(booking)
        return booking


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "booking", "status": "ok"}


@app.post("/quotes", response_model=QuoteView, status_code=201)
def create_quote(
    request: QuoteRequest,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> Quote:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    quote_data = _quote_selection(request, token)
    fingerprint = hashlib.sha256(
        json.dumps(quote_data["selection"], sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    quote = Quote(
        id=str(uuid.uuid4()),
        user_id=user_id,
        selection=quote_data["selection"],
        fingerprint=fingerprint,
        total_amount=Decimal(quote_data["total_amount"]),
        currency=quote_data["currency"],
        expires_at=utc_now() + QUOTE_TTL,
        status="open",
    )
    session.add(quote)
    session.commit()
    return quote


@app.get("/quotes/{quote_id}", response_model=QuoteView)
def get_quote(
    quote_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> Quote:
    quote = session.scalar(
        select(Quote).where(Quote.id == quote_id, Quote.user_id == user_id)
    )
    if quote is None:
        raise HTTPException(status_code=404, detail="Quote not found")
    return quote


def _get_or_create_booking(
    session: Session,
    *,
    request: StartBookingRequest,
    idempotency_key: str,
    user_id: str,
) -> str:
    request_fingerprint = hashlib.sha256(
        f"{user_id}:{request.quote_id}".encode("utf-8")
    ).hexdigest()
    try:
        with session.begin():
            existing = session.scalar(
                select(Booking).where(
                    Booking.user_id == user_id,
                    Booking.idempotency_key == idempotency_key,
                )
            )
            if existing:
                if existing.request_fingerprint != request_fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different booking",
                    )
                return existing.id
            quote = session.scalar(
                select(Quote)
                .where(Quote.id == request.quote_id, Quote.user_id == user_id)
                .with_for_update()
            )
            existing = session.scalar(
                select(Booking).where(
                    Booking.user_id == user_id,
                    Booking.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different booking",
                    )
                return existing.id
            if quote is None:
                raise HTTPException(status_code=404, detail="Quote not found")
            if quote.status != "open" or quote.expires_at <= utc_now():
                raise HTTPException(status_code=409, detail="Quote is no longer valid")
            booking = Booking(
                id=str(uuid.uuid4()),
                quote_id=quote.id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                status="starting",
                selection=quote.selection,
                total_amount=quote.total_amount,
                currency=quote.currency,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(booking)
            session.flush()
            quote.status = "booking"
            return booking.id
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(Booking).where(
                Booking.user_id == user_id,
                Booking.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        if existing.request_fingerprint != request_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was already used for a different booking",
            )
        return existing.id


@app.post("/bookings", response_model=BookingView, status_code=202)
def start_booking(
    request: StartBookingRequest,
    idempotency_key: str = Header(min_length=8, max_length=160),
    authorization: str | None = Header(default=None),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> dict[str, Any]:
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    booking_id = _get_or_create_booking(
        session,
        request=request,
        idempotency_key=idempotency_key,
        user_id=user_id,
    )
    booking = session.get(Booking, booking_id)
    session.commit()
    booking = _run_saga(session, booking, token)
    return _booking_view(session, booking)


@app.get("/bookings/{booking_id}", response_model=BookingView)
def get_booking(
    booking_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> dict[str, Any]:
    booking = session.scalar(
        select(Booking).where(Booking.id == booking_id, Booking.user_id == user_id)
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    return _booking_view(session, booking)


@app.post("/bookings/{booking_id}/resume", response_model=BookingView)
def resume_booking(
    booking_id: str,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> dict[str, Any]:
    booking = session.scalar(
        select(Booking).where(Booking.id == booking_id, Booking.user_id == user_id)
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    session.commit()
    booking = _run_saga(session, booking, token)
    return _booking_view(session, booking)


@app.post("/bookings/{booking_id}/reconcile", response_model=BookingView)
def reconcile_booking(
    booking_id: str,
    authorization: str | None = Header(default=None),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> dict[str, Any]:
    booking = session.scalar(
        select(Booking).where(Booking.id == booking_id, Booking.user_id == user_id)
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.status != "payment_unknown":
        return _booking_view(session, booking)
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    for operation in ("capture", "authorize"):
        step = _step(session, booking.id, f"payment:{operation}")
        if step is None or step.status not in {"succeeded", "unknown"}:
            continue
        if (
            step.status != "unknown"
            and (not step.result or step.result.get("status") != "unknown")
        ):
            continue
        try:
            payment = _call_service(
                "payment",
                "GET",
                f"payments/operations/{step.operation_key}",
                token=token,
            )
        except HTTPException as error:
            if error.status_code != 404:
                continue
            try:
                payment = _call_service(
                    "payment",
                    "POST",
                    f"payments/{operation}",
                    token=token,
                    body={
                        "booking_id": booking.id,
                        "amount": str(booking.total_amount),
                        "currency": booking.currency,
                    },
                    idempotency_key=step.operation_key,
                )
            except HTTPException:
                continue
        if payment["status"] == "unknown":
            continue
        if operation == "capture":
            if payment["status"] == "captured":
                _step_finish(session, step.id, status="succeeded", result=payment)
                _persist(session, booking.id, payment_capture_id=payment["id"], status="confirming_reservations")
                session.refresh(booking)
                booking = _run_saga(session, booking, token)
                break
            if payment["status"] == "failed":
                _persist(session, booking.id, status="compensating")
                _compensate(session, booking, token, "Payment capture failed after reconciliation")
                session.refresh(booking)
                break
        else:
            if payment["status"] == "authorized":
                _step_finish(session, step.id, status="succeeded", result=payment)
                _persist(
                    session,
                    booking.id,
                    payment_authorization_id=payment["id"],
                    status="capturing_payment",
                )
                session.refresh(booking)
                booking = _run_saga(session, booking, token)
                break
            if payment["status"] == "declined":
                _step_finish(session, step.id, status="succeeded", result=payment)
                _compensate(session, booking, token, "Payment authorization declined")
                session.refresh(booking)
                break
    return _booking_view(session, booking)
