from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Generator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.common.booking import HOLD_TTL, utc_now
from packages.common.database import (
    add_missing_columns,
    service_lifespan,
    session_dependency,
)
from packages.common.events import emit_catalog_snapshot, emit_reservation_state
from packages.common.security import current_user_id
from packages.seed_data import AIRLINE_NAMES, CITIES


class FlightBase(DeclarativeBase):
    pass


class Airline(FlightBase):
    __tablename__ = "airlines"

    code: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class FlightOffer(FlightBase):
    __tablename__ = "flight_offers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    airline_code: Mapped[str] = mapped_column(ForeignKey("airlines.code"), index=True)
    origin_city_code: Mapped[str] = mapped_column(String(3), index=True)
    destination_city_code: Mapped[str] = mapped_column(String(3), index=True)
    departure_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    arrival_at: Mapped[datetime] = mapped_column(DateTime)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    seats_available: Mapped[int] = mapped_column(Integer)


class FlightReservation(FlightBase):
    __tablename__ = "flight_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey("flight_offers.id"))
    passengers: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="confirmed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))


class AirlineView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str


class FlightOfferView(BaseModel):
    id: str
    airline_code: str
    airline_name: str
    origin_city_code: str
    destination_city_code: str
    departure_at: datetime
    arrival_at: datetime
    price: Decimal
    currency: str
    seats_available: int


class FlightReservationRequest(BaseModel):
    offer_id: str = Field(min_length=1, max_length=36)
    passengers: int = Field(ge=1, le=9)


class FlightReservationView(BaseModel):
    id: str
    offer_id: str
    passengers: int
    status: str
    expires_at: datetime | None
    total_price: Decimal
    currency: str


def _seed(session: Session) -> None:
    if session.scalar(select(Airline.code).limit(1)) is not None:
        return
    airline_rows = [
        Airline(code=f"airline-{index + 1:02}", name=f"{name} {index + 1:02}")
        for index, name in enumerate(
            AIRLINE_NAMES[index % len(AIRLINE_NAMES)] for index in range(30)
        )
    ]
    session.add_all(airline_rows)
    session.flush()

    today = date.today()
    city_count = len(CITIES)
    offers: list[FlightOffer] = []
    for index in range(100):
        origin_index = index % city_count
        destination_index = (origin_index + 1 + index % (city_count - 1)) % city_count
        departure_day = today + timedelta(days=index % 30 + 1)
        departure = datetime.combine(
            departure_day, time(hour=6 + index % 14, minute=(index * 7) % 60)
        )
        offers.append(
            FlightOffer(
                id=f"offer-{index + 1:03}",
                airline_code=f"airline-{index % 30 + 1:02}",
                origin_city_code=CITIES[origin_index][0],
                destination_city_code=CITIES[destination_index][0],
                departure_at=departure,
                arrival_at=departure + timedelta(hours=2 + index % 13),
                price=Decimal(120 + (index * 47) % 1180),
                currency="USD",
                seats_available=1 + index % 8,
            )
        )
    session.add_all(offers)
    session.commit()


def _catalog_snapshot(session: Session) -> None:
    rows = session.execute(
        select(FlightOffer, Airline.name).join(Airline).order_by(FlightOffer.id)
    )
    documents = [
        {
            "id": offer.id,
            "kind": "flight",
            "name": f"{airline_name} {offer.origin_city_code}-{offer.destination_city_code}",
            "origin_city_code": offer.origin_city_code,
            "destination_city_code": offer.destination_city_code,
            "departure_at": offer.departure_at,
            "arrival_at": offer.arrival_at,
            "price": offer.price,
            "currency": offer.currency,
            "seats_available": offer.seats_available,
        }
        for offer, airline_name in rows
    ]
    emit_catalog_snapshot(session, producer="flight", documents=documents)


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


def _fingerprint(user_id: str, offer_id: str, passengers: int) -> str:
    payload = json.dumps(
        {"user_id": user_id, "offer_id": offer_id, "passengers": passengers},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _migrate(engine: Engine) -> None:
    add_missing_columns(
        engine,
        "flight_reservations",
        {"expires_at": "TIMESTAMP", "version": "INTEGER NOT NULL DEFAULT 1"},
    )


def _expire_holds(session: Session, now: datetime) -> None:
    expired = list(
        session.scalars(
            select(FlightReservation)
            .where(
                FlightReservation.status == "held",
                FlightReservation.expires_at <= now,
            )
            .with_for_update()
        )
    )
    for reservation in expired:
        changed = session.execute(
            update(FlightReservation)
            .where(
                FlightReservation.id == reservation.id,
                FlightReservation.status == "held",
                FlightReservation.expires_at <= now,
            )
            .values(status="expired")
        )
        if changed.rowcount == 1:
            reservation.version += 1
            emit_reservation_state(
                session,
                producer="flight",
                reservation_id=reservation.id,
                resource_type="flight",
                resource_id=reservation.offer_id,
                status="expired",
                version=reservation.version,
            )
            session.execute(
                update(FlightOffer)
                .where(FlightOffer.id == reservation.offer_id)
                .values(
                    seats_available=(
                        FlightOffer.seats_available + reservation.passengers
                    )
                )
            )


app = FastAPI(
    title="Flight Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=FlightBase,
        seed=_seed,
        default_database_url="sqlite:///./flight.db",
        migrate=_migrate,
        events_producer="flight",
        bootstrap_events=_catalog_snapshot,
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "flight", "status": "ok"}


@app.get("/airlines", response_model=list[AirlineView])
def list_airlines(session: Session = Depends(_session)) -> list[Airline]:
    return list(session.scalars(select(Airline).order_by(Airline.name)))


@app.get("/flights/search", response_model=list[FlightOfferView])
def search_flights(
    origin: str = Query(min_length=3, max_length=3),
    destination: str = Query(min_length=3, max_length=3),
    departure_date: date | None = None,
    passengers: int = Query(default=1, ge=1, le=9),
    session: Session = Depends(_session),
) -> list[dict[str, Any]]:
    statement = (
        select(FlightOffer, Airline.name)
        .join(Airline, Airline.code == FlightOffer.airline_code)
        .where(
            FlightOffer.origin_city_code == origin.upper(),
            FlightOffer.destination_city_code == destination.upper(),
            FlightOffer.seats_available >= passengers,
        )
    )
    if departure_date:
        day_start = datetime.combine(departure_date, time.min)
        statement = statement.where(
            FlightOffer.departure_at >= day_start,
            FlightOffer.departure_at < day_start + timedelta(days=1),
        )
    with session.begin():
        _expire_holds(session, utc_now())
        results = session.execute(
            statement.order_by(FlightOffer.price, FlightOffer.departure_at)
        )
        return [
            {
                "id": offer.id,
                "airline_code": offer.airline_code,
                "airline_name": airline_name,
                "origin_city_code": offer.origin_city_code,
                "destination_city_code": offer.destination_city_code,
                "departure_at": offer.departure_at,
                "arrival_at": offer.arrival_at,
                "price": offer.price,
                "currency": offer.currency,
                "seats_available": offer.seats_available,
            }
            for offer, airline_name in results
        ]


@app.post("/reservations", response_model=FlightReservationView, status_code=201)
def reserve_flight(
    request: FlightReservationRequest,
    idempotency_key: str = Header(min_length=8, max_length=128),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> FlightReservation:
    fingerprint = _fingerprint(user_id, request.offer_id, request.passengers)
    now = utc_now()
    try:
        with session.begin():
            _expire_holds(session, now)
            existing = session.scalar(
                select(FlightReservation).where(
                    FlightReservation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different request",
                    )
                return existing

            offer = session.get(FlightOffer, request.offer_id)
            if offer is None:
                raise HTTPException(status_code=404, detail="Flight offer not found")
            reserved = session.execute(
                update(FlightOffer)
                .where(
                    FlightOffer.id == request.offer_id,
                    FlightOffer.seats_available >= request.passengers,
                )
                .values(seats_available=FlightOffer.seats_available - request.passengers)
            )
            if reserved.rowcount != 1:
                raise HTTPException(status_code=409, detail="Not enough seats available")

            reservation = FlightReservation(
                id=str(uuid.uuid4()),
                user_id=user_id,
                offer_id=request.offer_id,
                passengers=request.passengers,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status="held",
                expires_at=now + HOLD_TTL,
                total_price=offer.price * request.passengers,
                currency=offer.currency,
            )
            session.add(reservation)
            session.flush()
            emit_reservation_state(
                session,
                producer="flight",
                reservation_id=reservation.id,
                resource_type="flight",
                resource_id=reservation.offer_id,
                status=reservation.status,
                version=reservation.version,
            )
            return reservation
    except IntegrityError as error:
        session.rollback()
        existing = session.scalar(
            select(FlightReservation).where(
                FlightReservation.idempotency_key == idempotency_key
            )
        )
        if existing and existing.request_fingerprint == fingerprint:
            return existing
        raise HTTPException(status_code=409, detail="Reservation conflicts with current state") from error


@app.post(
    "/reservations/{reservation_id}/confirm",
    response_model=FlightReservationView,
)
def confirm_flight_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> FlightReservation:
    now = utc_now()
    failure: str | None = None
    reservation: FlightReservation
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(FlightReservation)
            .where(
                FlightReservation.id == reservation_id,
                FlightReservation.user_id == user_id,
            )
            .with_for_update()
        )
        if reservation is None:
            raise HTTPException(status_code=404, detail="Reservation not found")
        if reservation.status == "confirmed":
            return reservation
        if reservation.status != "held":
            failure = "Reservation is no longer available to confirm"
        else:
            changed = session.execute(
                update(FlightReservation)
                .where(
                    FlightReservation.id == reservation.id,
                    FlightReservation.status == "held",
                    FlightReservation.expires_at > now,
                )
                .values(status="confirmed", expires_at=None)
            )
            if changed.rowcount == 1:
                reservation.status = "confirmed"
                reservation.expires_at = None
                reservation.version += 1
                emit_reservation_state(
                    session,
                    producer="flight",
                    reservation_id=reservation.id,
                    resource_type="flight",
                    resource_id=reservation.offer_id,
                    status=reservation.status,
                    version=reservation.version,
                )
            else:
                session.refresh(reservation)
                if reservation.status != "confirmed":
                    failure = "Reservation is no longer available to confirm"
    if failure:
        raise HTTPException(status_code=409, detail=failure)
    return reservation


@app.delete(
    "/reservations/{reservation_id}",
    response_model=FlightReservationView,
)
def cancel_flight_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> FlightReservation:
    now = utc_now()
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(FlightReservation)
            .where(
                FlightReservation.id == reservation_id,
                FlightReservation.user_id == user_id,
            )
            .with_for_update()
        )
        if reservation is None:
            raise HTTPException(status_code=404, detail="Reservation not found")
        if reservation.status in {"cancelled", "expired"}:
            return reservation
        if reservation.status not in {"held", "confirmed"}:
            raise HTTPException(status_code=409, detail="Reservation cannot be cancelled")
        changed = session.execute(
            update(FlightReservation)
            .where(
                FlightReservation.id == reservation.id,
                FlightReservation.user_id == user_id,
                FlightReservation.status.in_(("held", "confirmed")),
            )
            .values(status="cancelled", expires_at=None)
        )
        if changed.rowcount != 1:
            session.refresh(reservation)
            if reservation.status in {"cancelled", "expired"}:
                return reservation
            raise HTTPException(status_code=409, detail="Reservation cannot be cancelled")
        reservation.status = "cancelled"
        reservation.expires_at = None
        reservation.version += 1
        emit_reservation_state(
            session,
            producer="flight",
            reservation_id=reservation.id,
            resource_type="flight",
            resource_id=reservation.offer_id,
            status=reservation.status,
            version=reservation.version,
        )
        session.execute(
            update(FlightOffer)
            .where(FlightOffer.id == reservation.offer_id)
            .values(
                seats_available=FlightOffer.seats_available + reservation.passengers
            )
        )
        return reservation
