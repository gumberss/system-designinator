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
from packages.seed_data import ACTIVITY_NAMES, CITIES


class ActivitiesBase(DeclarativeBase):
    pass


class Activity(ActivitiesBase):
    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    city_code: Mapped[str] = mapped_column(String(3), index=True)
    description: Mapped[str] = mapped_column(String(500))
    duration_minutes: Mapped[int] = mapped_column(Integer)


class ActivitySchedule(ActivitiesBase):
    __tablename__ = "activity_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"), index=True)
    schedule_date: Mapped[date] = mapped_column(Date, index=True)
    total_capacity: Mapped[int] = mapped_column(Integer)
    remaining_capacity: Mapped[int] = mapped_column(Integer)
    price_per_person: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")


class ActivityReservation(ActivitiesBase):
    __tablename__ = "activity_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    activity_id: Mapped[str] = mapped_column(ForeignKey("activities.id"))
    schedule_id: Mapped[str] = mapped_column(ForeignKey("activity_schedules.id"))
    participants: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="confirmed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))


class ActivityReservationRequest(BaseModel):
    schedule_id: str = Field(min_length=1, max_length=36)
    participants: int = Field(ge=1, le=12)


class ActivityReservationView(BaseModel):
    id: str
    activity_id: str
    schedule_id: str
    participants: int
    status: str
    expires_at: datetime | None
    total_price: Decimal
    currency: str


def _seed(session: Session) -> None:
    if session.scalar(select(Activity.id).limit(1)) is not None:
        return
    activities = [
        Activity(
            id=f"activity-{index + 1:03}",
            name=f"{CITIES[index % len(CITIES)][1]} {ACTIVITY_NAMES[index % len(ACTIVITY_NAMES)]}",
            city_code=CITIES[index % len(CITIES)][0],
            description=(
                f"A simulated small-group experience in "
                f"{CITIES[index % len(CITIES)][1]}."
            ),
            duration_minutes=60 + (index % 5) * 30,
        )
        for index in range(100)
    ]
    session.add_all(activities)
    session.flush()
    today = date.today()
    schedules = [
        ActivitySchedule(
            id=f"schedule-{activity_index + 1:03}-{day_offset:02}",
            activity_id=f"activity-{activity_index + 1:03}",
            schedule_date=today + timedelta(days=day_offset),
            total_capacity=8 + (activity_index + day_offset) % 13,
            remaining_capacity=8 + (activity_index + day_offset) % 13,
            price_per_person=Decimal(
                25 + (activity_index * 7 + day_offset * 3) % 180
            ),
            currency="USD",
        )
        for activity_index in range(100)
        for day_offset in range(1, 61)
    ]
    session.add_all(schedules)
    session.commit()


def _catalog_snapshot(session: Session) -> None:
    rows = session.execute(
        select(
            Activity,
            func.min(ActivitySchedule.price_per_person),
            func.min(ActivitySchedule.currency),
        )
        .join(ActivitySchedule, ActivitySchedule.activity_id == Activity.id)
        .group_by(Activity.id)
        .order_by(Activity.id)
    )
    documents = [
        {
            "id": activity.id,
            "kind": "activity",
            "name": activity.name,
            "city_code": activity.city_code,
            "description": activity.description,
            "duration_minutes": activity.duration_minutes,
            "starting_price": starting_price,
            "currency": currency,
        }
        for activity, starting_price, currency in rows
    ]
    emit_catalog_snapshot(session, producer="activities", documents=documents)


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


def _fingerprint(
    user_id: str, schedule_id: str, participants: int
) -> str:
    payload = {
        "user_id": user_id,
        "schedule_id": schedule_id,
        "participants": participants,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _migrate(engine: Engine) -> None:
    add_missing_columns(
        engine,
        "activity_reservations",
        {"expires_at": "TIMESTAMP", "version": "INTEGER NOT NULL DEFAULT 1"},
    )


def _release_capacity(session: Session, reservation: ActivityReservation) -> None:
    changed = session.execute(
        update(ActivitySchedule)
        .where(
            ActivitySchedule.id == reservation.schedule_id,
            ActivitySchedule.remaining_capacity
            <= ActivitySchedule.total_capacity - reservation.participants,
        )
        .values(
            remaining_capacity=(
                ActivitySchedule.remaining_capacity + reservation.participants
            )
        )
    )
    if changed.rowcount != 1:
        raise HTTPException(
            status_code=409, detail="Reservation capacity cannot be released"
        )


def _expire_holds(session: Session, now: datetime) -> None:
    expired = list(
        session.scalars(
            select(ActivityReservation)
            .where(
                ActivityReservation.status == "held",
                ActivityReservation.expires_at <= now,
            )
            .with_for_update()
        )
    )
    for reservation in expired:
        changed = session.execute(
            update(ActivityReservation)
            .where(
                ActivityReservation.id == reservation.id,
                ActivityReservation.status == "held",
                ActivityReservation.expires_at <= now,
            )
            .values(status="expired")
        )
        if changed.rowcount == 1:
            reservation.version += 1
            emit_reservation_state(
                session,
                producer="activities",
                reservation_id=reservation.id,
                resource_type="activity_schedule",
                resource_id=reservation.schedule_id,
                status="expired",
                version=reservation.version,
            )
            _release_capacity(session, reservation)


app = FastAPI(
    title="Activities Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=ActivitiesBase,
        seed=_seed,
        default_database_url="sqlite:///./activities.db",
        migrate=_migrate,
        events_producer="activities",
        bootstrap_events=_catalog_snapshot,
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "activities", "status": "ok"}


@app.get("/activities/search")
def search_activities(
    city_code: str = Query(min_length=3, max_length=3),
    activity_date: date | None = None,
    session: Session = Depends(_session),
) -> list[dict[str, Any]]:
    _expire_holds(session, utc_now())
    session.commit()
    statement = (
        select(Activity, ActivitySchedule)
        .join(ActivitySchedule, ActivitySchedule.activity_id == Activity.id)
        .where(Activity.city_code == city_code.upper())
    )
    if activity_date:
        statement = statement.where(ActivitySchedule.schedule_date == activity_date)
    statement = statement.where(ActivitySchedule.remaining_capacity > 0).order_by(
        Activity.name, ActivitySchedule.schedule_date
    )
    return [
        {
            "activity_id": activity.id,
            "name": activity.name,
            "city_code": activity.city_code,
            "description": activity.description,
            "duration_minutes": activity.duration_minutes,
            "schedule_id": schedule.id,
            "schedule_date": schedule.schedule_date,
            "remaining_capacity": schedule.remaining_capacity,
            "price_per_person": schedule.price_per_person,
            "currency": schedule.currency,
        }
        for activity, schedule in session.execute(statement)
    ]


@app.post("/reservations", response_model=ActivityReservationView, status_code=201)
def reserve_activity(
    request: ActivityReservationRequest,
    idempotency_key: str = Header(min_length=8, max_length=128),
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> ActivityReservation:
    fingerprint = _fingerprint(user_id, request.schedule_id, request.participants)
    now = utc_now()
    try:
        with session.begin():
            _expire_holds(session, now)
            existing = session.scalar(
                select(ActivityReservation).where(
                    ActivityReservation.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=409,
                        detail="Idempotency key was already used for a different request",
                    )
                return existing

            schedule = session.get(ActivitySchedule, request.schedule_id)
            if schedule is None:
                raise HTTPException(status_code=404, detail="Activity schedule not found")
            changed = session.execute(
                update(ActivitySchedule)
                .where(
                    ActivitySchedule.id == request.schedule_id,
                    ActivitySchedule.remaining_capacity >= request.participants,
                )
                .values(
                    remaining_capacity=(
                        ActivitySchedule.remaining_capacity - request.participants
                    )
                )
            )
            if changed.rowcount != 1:
                raise HTTPException(
                    status_code=409, detail="Not enough activity capacity available"
                )
            reservation = ActivityReservation(
                id=str(uuid.uuid4()),
                user_id=user_id,
                activity_id=schedule.activity_id,
                schedule_id=schedule.id,
                participants=request.participants,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status="held",
                expires_at=now + HOLD_TTL,
                total_price=schedule.price_per_person * request.participants,
                currency=schedule.currency,
            )
            session.add(reservation)
            session.flush()
            emit_reservation_state(
                session,
                producer="activities",
                reservation_id=reservation.id,
                resource_type="activity_schedule",
                resource_id=reservation.schedule_id,
                status=reservation.status,
                version=reservation.version,
            )
            return reservation
    except IntegrityError as error:
        session.rollback()
        existing = session.scalar(
            select(ActivityReservation).where(
                ActivityReservation.idempotency_key == idempotency_key
            )
        )
        if existing and existing.request_fingerprint == fingerprint:
            return existing
        raise HTTPException(status_code=409, detail="Reservation conflicts with current state") from error


@app.post(
    "/reservations/{reservation_id}/confirm",
    response_model=ActivityReservationView,
)
def confirm_activity_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> ActivityReservation:
    now = utc_now()
    failure: str | None = None
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(ActivityReservation)
            .where(
                ActivityReservation.id == reservation_id,
                ActivityReservation.user_id == user_id,
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
                update(ActivityReservation)
                .where(
                    ActivityReservation.id == reservation.id,
                    ActivityReservation.status == "held",
                    ActivityReservation.expires_at > now,
                )
                .values(status="confirmed", expires_at=None)
            )
            if changed.rowcount == 1:
                reservation.status = "confirmed"
                reservation.expires_at = None
                reservation.version += 1
                emit_reservation_state(
                    session,
                    producer="activities",
                    reservation_id=reservation.id,
                    resource_type="activity_schedule",
                    resource_id=reservation.schedule_id,
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
    response_model=ActivityReservationView,
)
def cancel_activity_reservation(
    reservation_id: str,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> ActivityReservation:
    now = utc_now()
    with session.begin():
        _expire_holds(session, now)
        reservation = session.scalar(
            select(ActivityReservation)
            .where(
                ActivityReservation.id == reservation_id,
                ActivityReservation.user_id == user_id,
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
            update(ActivityReservation)
            .where(
                ActivityReservation.id == reservation.id,
                ActivityReservation.user_id == user_id,
                ActivityReservation.status.in_(("held", "confirmed")),
            )
            .values(status="cancelled", expires_at=None)
        )
        if changed.rowcount != 1:
            session.refresh(reservation)
            if reservation.status in {"cancelled", "expired"}:
                return reservation
            raise HTTPException(status_code=409, detail="Reservation cannot be cancelled")
        _release_capacity(session, reservation)
        reservation.status = "cancelled"
        reservation.expires_at = None
        reservation.version += 1
        emit_reservation_state(
            session,
            producer="activities",
            reservation_id=reservation.id,
            resource_type="activity_schedule",
            resource_id=reservation.schedule_id,
            status=reservation.status,
            version=reservation.version,
        )
        return reservation
