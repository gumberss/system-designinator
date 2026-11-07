from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    and_,
    func,
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
from packages.seed_data import CITIES, HOTEL_NAMES


class HotelBase(DeclarativeBase):
    pass


class Hotel(HotelBase):
    __tablename__ = "hotels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    city_code: Mapped[str] = mapped_column(String(3), index=True)
    rating: Mapped[int] = mapped_column(Integer)


class RoomType(HotelBase):
    __tablename__ = "room_types"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    max_guests: Mapped[int] = mapped_column(Integer)
    price_per_night: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class RoomInventory(HotelBase):
    __tablename__ = "room_inventory"

    room_type_id: Mapped[str] = mapped_column(
        ForeignKey("room_types.id"), primary_key=True
    )
    inventory_date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_units: Mapped[int] = mapped_column(Integer)
    remaining_units: Mapped[int] = mapped_column(Integer)


class HotelReservation(HotelBase):
    __tablename__ = "hotel_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    room_type_id: Mapped[str] = mapped_column(ForeignKey("room_types.id"))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    units: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="confirmed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))


class HotelReservationRequest(BaseModel):
    hotel_id: str = Field(min_length=1, max_length=36)
    room_type_id: str = Field(min_length=1, max_length=36)
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=5)
    guests: int = Field(default=1, ge=1, le=8)


class HotelReservationView(BaseModel):
    id: str
    hotel_id: str
    room_type_id: str
    check_in: date
    check_out: date
    units: int
    status: str
    expires_at: datetime | None
    total_price: Decimal
    currency: str


def _seed(session: Session) -> None:
    if session.scalar(select(Hotel.id).limit(1)) is not None:
        return
    today = date.today()
    hotels: list[Hotel] = []
    room_types: list[RoomType] = []
    inventory: list[RoomInventory] = []
    for index in range(50):
        hotel_id = f"hotel-{index + 1:03}"
        city_code = CITIES[index % len(CITIES)][0]
        hotels.append(
            Hotel(
                id=hotel_id,
                name=f"{HOTEL_NAMES[index % len(HOTEL_NAMES)]} {index + 1:02}",
                city_code=city_code,
                rating=3 + index % 3,
            )
        )
        for room_index, room_name in enumerate(("Standard", "Family"), start=1):
            room_type_id = f"{hotel_id}-room-{room_index}"
            room_types.append(
                RoomType(
                    id=room_type_id,
                    hotel_id=hotel_id,
                    name=room_name,
                    max_guests=2 if room_index == 1 else 4,
                    price_per_night=Decimal(
                        80 + (index * 19 + room_index * 45) % 420
                    ),
                    currency="USD",
                )
            )
            inventory.extend(
                RoomInventory(
                    room_type_id=room_type_id,
                    inventory_date=today + timedelta(days=offset),
                    total_units=3 + (index + offset) % 8,
                    remaining_units=3 + (index + offset) % 8,
                )
                for offset in range(1, 91)
            )
    session.add_all(hotels)
    session.flush()
    session.add_all(room_types)
    session.flush()
    session.add_all(inventory)
    session.commit()


def _catalog_snapshot(session: Session) -> None:
    rows = session.execute(
        select(
            Hotel,
            func.min(RoomType.price_per_night),
            func.min(RoomType.currency),
        )
        .join(RoomType, RoomType.hotel_id == Hotel.id)
        .group_by(Hotel.id)
        .order_by(Hotel.id)
    )
    documents = [
        {
            "id": hotel.id,
            "kind": "hotel",
            "name": hotel.name,
            "city_code": hotel.city_code,
            "rating": hotel.rating,
            "starting_price": starting_price,
            "currency": currency,
        }
        for hotel, starting_price, currency in rows
    ]
    emit_catalog_snapshot(session, producer="hotel", documents=documents)


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


def _fingerprint(user_id: str, request: HotelReservationRequest) -> str:
    payload = {
        "user_id": user_id,
        "hotel_id": request.hotel_id,
        "room_type_id": request.room_type_id,
        "check_in": request.check_in.isoformat(),
        "check_out": request.check_out.isoformat(),
        "units": request.units,
        "guests": request.guests,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _migrate(engine: Engine) -> None:
    add_missing_columns(
        engine,
        "hotel_reservations",
        {"expires_at": "TIMESTAMP", "version": "INTEGER NOT NULL DEFAULT 1"},
    )


def _release_inventory(session: Session, reservation: HotelReservation) -> None:
    inventory_rows = list(
        session.scalars(
            select(RoomInventory)
            .where(
                RoomInventory.room_type_id == reservation.room_type_id,
                RoomInventory.inventory_date >= reservation.check_in,
                RoomInventory.inventory_date < reservation.check_out,
            )
            .with_for_update()
        )
    )
    nights = (reservation.check_out - reservation.check_in).days
    if len(inventory_rows) != nights:
        raise HTTPException(
            status_code=409, detail="Reservation inventory is unavailable"
        )
    for row in inventory_rows:
        changed = session.execute(
            update(RoomInventory)
            .where(
                RoomInventory.room_type_id == row.room_type_id,
                RoomInventory.inventory_date == row.inventory_date,
                RoomInventory.remaining_units
                <= RoomInventory.total_units - reservation.units,
            )
            .values(remaining_units=RoomInventory.remaining_units + reservation.units)
        )
        if changed.rowcount != 1:
            raise HTTPException(
                status_code=409, detail="Reservation inventory cannot be released"
            )


def _expire_holds(session: Session, now: datetime) -> None:
    expired = list(
        session.scalars(
            select(HotelReservation)
            .where(
                HotelReservation.status == "held",
                HotelReservation.expires_at <= now,
            )
            .with_for_update()
        )
    )
    for reservation in expired:
        changed = session.execute(
            update(HotelReservation)
            .where(
                HotelReservation.id == reservation.id,
                HotelReservation.status == "held",
                HotelReservation.expires_at <= now,
            )
            .values(status="expired")
        )
        if changed.rowcount == 1:
            reservation.version += 1
            emit_reservation_state(
                session,
                producer="hotel",
                reservation_id=reservation.id,
                resource_type="hotel_room",
                resource_id=reservation.room_type_id,
                status="expired",
                version=reservation.version,
            )
            _release_inventory(session, reservation)


app = FastAPI(
    title="Hotel Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=HotelBase,
        seed=_seed,
        default_database_url="sqlite:///./hotel.db",
        migrate=_migrate,
        events_producer="hotel",
        bootstrap_events=_catalog_snapshot,
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "hotel", "status": "ok"}


@app.get("/hotels/search")
def search_hotels(
    check_in: date,
    check_out: date,
    city_code: str = Query(min_length=3, max_length=3),
    guests: int = Query(default=1, ge=1, le=8),
    session: Session = Depends(_session),
) -> list[dict[str, Any]]:
    if check_out <= check_in or (check_out - check_in).days > 30:
        raise HTTPException(status_code=422, detail="Invalid stay date range")
    _expire_holds(session, utc_now())
    session.commit()
    nights = (check_out - check_in).days
    statement = (
        select(Hotel, RoomType)
        .join(RoomType, RoomType.hotel_id == Hotel.id)
        .where(
            Hotel.city_code == city_code.upper(),
            RoomType.max_guests >= guests,
        )
        .order_by(Hotel.name, RoomType.price_per_night)
    )
    matches: list[dict[str, Any]] = []
    for hotel, room_type in session.execute(statement):
        rows = list(
            session.scalars(
                select(RoomInventory).where(
                    RoomInventory.room_type_id == room_type.id,
                    RoomInventory.inventory_date >= check_in,
                    RoomInventory.inventory_date < check_out,
                )
            )
        )
        if len(rows) == nights and all(row.remaining_units >= 1 for row in rows):
            matches.append(
                {
                    "hotel_id": hotel.id,
                    "hotel_name": hotel.name,
                    "city_code": hotel.city_code,
                    "rating": hotel.rating,
                    "room_type_id": room_type.id,
                    "room_type": room_type.name,
                    "max_guests": room_type.max_guests,
                    "price_per_night": room_type.price_per_night,
                    "currency": room_type.currency,
                    "available_units": min(row.remaining_units for row in rows),
                }
            )
    return matches


@app.post("/reservations", response_model=HotelReservationView, status_code=201)
def reserve_hotel(
    request: HotelReservationRequest,
    idempotency_key: str = Header(min_length=8, max_length=128),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> HotelReservation:
    if request.check_out <= request.check_in or (request.check_out - request.check_in).days > 30:
        raise HTTPException(status_code=422, detail="Invalid stay date range")
    fingerprint = _fingerprint(user_id, request)
    now = utc_now()
    try:
        with session.begin():
            _expire_holds(session, now)
            existing = session.scalar(
                select(HotelReservation).where(
                    HotelReservation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different request",
                    )
                return existing

            room_type = session.scalar(
                select(RoomType).where(
                    RoomType.id == request.room_type_id,
                    RoomType.hotel_id == request.hotel_id,
                )
            )
            if room_type is None:
                raise HTTPException(status_code=404, detail="Room type not found")
            if room_type.max_guests < request.guests:
                raise HTTPException(status_code=409, detail="Room capacity is too small")
            nights = (request.check_out - request.check_in).days
            inventory_rows = list(
                session.scalars(
                    select(RoomInventory)
                    .where(
                        RoomInventory.room_type_id == room_type.id,
                        RoomInventory.inventory_date >= request.check_in,
                        RoomInventory.inventory_date < request.check_out,
                    )
                    .with_for_update()
                )
            )
            if len(inventory_rows) != nights:
                raise HTTPException(
                    status_code=409, detail="Inventory is unavailable for these dates"
                )
            for row in inventory_rows:
                changed = session.execute(
                    update(RoomInventory)
                    .where(
                        and_(
                            RoomInventory.room_type_id == row.room_type_id,
                            RoomInventory.inventory_date == row.inventory_date,
                            RoomInventory.remaining_units >= request.units,
                        )
                    )
                    .values(
                        remaining_units=RoomInventory.remaining_units - request.units
                    )
                )
                if changed.rowcount != 1:
                    raise HTTPException(
                        status_code=409, detail="Room inventory is no longer available"
                    )
            reservation = HotelReservation(
                id=str(uuid.uuid4()),
                user_id=user_id,
                hotel_id=request.hotel_id,
                room_type_id=room_type.id,
                check_in=request.check_in,
                check_out=request.check_out,
                units=request.units,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status="held",
                expires_at=now + HOLD_TTL,
                total_price=room_type.price_per_night * request.units * nights,
                currency=room_type.currency,
            )
            session.add(reservation)
            session.flush()
            emit_reservation_state(
                session,
                producer="hotel",
                reservation_id=reservation.id,
                resource_type="hotel_room",
                resource_id=reservation.room_type_id,
                status=reservation.status,
                version=reservation.version,
            )
            return reservation
    except IntegrityError as error:
        session.rollback()
        existing = session.scalar(
            select(HotelReservation).where(
                HotelReservation.idempotency_key == idempotency_key
            )
        )
        if existing and existing.request_fingerprint == fingerprint:
            return existing
        raise HTTPException(status_code=409, detail="Reservation conflicts with current state") from error


@app.post(
    "/reservations/{reservation_id}/confirm",
    response_model=HotelReservationView,
)
def confirm_hotel_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> HotelReservation:
    now = utc_now()
    failure: str | None = None
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(HotelReservation)
            .where(
                HotelReservation.id == reservation_id,
                HotelReservation.user_id == user_id,
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
                update(HotelReservation)
                .where(
                    HotelReservation.id == reservation.id,
                    HotelReservation.status == "held",
                    HotelReservation.expires_at > now,
                )
                .values(status="confirmed", expires_at=None)
            )
            if changed.rowcount == 1:
                reservation.status = "confirmed"
                reservation.expires_at = None
                reservation.version += 1
                emit_reservation_state(
                    session,
                    producer="hotel",
                    reservation_id=reservation.id,
                    resource_type="hotel_room",
                    resource_id=reservation.room_type_id,
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
    response_model=HotelReservationView,
)
def cancel_hotel_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> HotelReservation:
    now = utc_now()
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(HotelReservation)
            .where(
                HotelReservation.id == reservation_id,
                HotelReservation.user_id == user_id,
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
            update(HotelReservation)
            .where(
                HotelReservation.id == reservation.id,
                HotelReservation.user_id == user_id,
                HotelReservation.status.in_(("held", "confirmed")),
            )
            .values(status="cancelled", expires_at=None)
        )
        if changed.rowcount != 1:
            session.refresh(reservation)
            if reservation.status in {"cancelled", "expired"}:
                return reservation
            raise HTTPException(status_code=409, detail="Reservation cannot be cancelled")
        _release_inventory(session, reservation)
        reservation.status = "cancelled"
        reservation.expires_at = None
        reservation.version += 1
        emit_reservation_state(
            session,
            producer="hotel",
            reservation_id=reservation.id,
            resource_type="hotel_room",
            resource_id=reservation.room_type_id,
            status=reservation.status,
            version=reservation.version,
        )
        return reservation
