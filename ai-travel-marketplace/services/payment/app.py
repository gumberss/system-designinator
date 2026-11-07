from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Numeric, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from packages.common.database import service_lifespan, session_dependency
from packages.common.events import emit_event


class PaymentBase(DeclarativeBase):
    pass


class PaymentOperation(PaymentBase):
    __tablename__ = "payment_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    booking_id: Mapped[str] = mapped_column(String(36), index=True)
    operation_key: Mapped[str] = mapped_column(String(160), unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))


class PaymentRequest(BaseModel):
    booking_id: str = Field(min_length=1, max_length=36)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)


class PaymentView(BaseModel):
    id: str
    booking_id: str
    operation: str
    status: str
    amount: Decimal
    currency: str


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


app = FastAPI(
    title="Simulated Payment Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=PaymentBase,
        seed=lambda session: None,
        default_database_url="sqlite:///./payment.db",
        events_producer="payment",
    ),
)


def _response_status(operation: str, stored_status: str) -> str:
    mode = os.getenv("PAYMENT_SIMULATOR_MODE", "normal")
    if operation == "authorize":
        if mode == "decline_authorization":
            return "declined"
        if mode == "unknown_authorization" and stored_status == "authorized":
            return "unknown"
    if operation == "capture" and mode == "unknown_capture" and stored_status == "captured":
        return "unknown"
    if operation == "refund" and mode == "fail_refund":
        return "failed"
    return stored_status


def _perform(
    operation: str,
    request: PaymentRequest,
    operation_key: str,
    session: Session,
) -> PaymentOperation:
    if operation_key is None:
        raise HTTPException(status_code=422, detail="Idempotency-Key is required")
    fingerprint = f"{request.booking_id}:{request.amount}:{request.currency}:{operation}"
    with session.begin():
        existing = session.scalar(
            select(PaymentOperation).where(
                PaymentOperation.operation_key == operation_key
            )
        )
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Payment operation key was used with a different request",
                )
            return existing

        prior = list(
            session.scalars(
                select(PaymentOperation).where(
                    PaymentOperation.booking_id == request.booking_id
                )
            )
        )
        authorized = any(item.operation == "authorize" and item.status == "authorized" for item in prior)
        captured = any(item.operation == "capture" and item.status == "captured" for item in prior)
        if operation == "capture" and not authorized:
            raise HTTPException(status_code=409, detail="Payment is not authorized")
        if operation in {"void", "refund"} and not (authorized or captured):
            raise HTTPException(status_code=409, detail="No payment is available to reverse")
        if operation == "refund" and not captured:
            raise HTTPException(status_code=409, detail="Payment has not been captured")
        status = "authorized" if operation == "authorize" else {
            "capture": "captured",
            "void": "voided",
            "refund": "refunded",
        }[operation]
        if operation == "authorize" and os.getenv(
            "PAYMENT_SIMULATOR_MODE", "normal"
        ) == "decline_authorization":
            status = "declined"
        payment = PaymentOperation(
            id=str(uuid.uuid4()),
            booking_id=request.booking_id,
            operation_key=operation_key,
            request_fingerprint=fingerprint,
            operation=operation,
            status=status,
            amount=request.amount,
            currency=request.currency.upper(),
        )
        session.add(payment)
        session.flush()
        event_type = {
            ("authorize", "authorized"): "PaymentAuthorizationSucceeded",
            ("authorize", "declined"): "PaymentAuthorizationDeclined",
            ("capture", "captured"): "PaymentCaptureSucceeded",
            ("void", "voided"): "PaymentVoided",
            ("refund", "refunded"): "PaymentRefunded",
        }.get((operation, status), "PaymentOperationFailed")
        emit_event(
            session,
            producer="payment",
            event_type=event_type,
            aggregate_type="payment",
            aggregate_id=request.booking_id,
            aggregate_version=len(prior) + 1,
            causation_id=operation_key,
            payload={
                "booking_id": request.booking_id,
                "payment_id": payment.id,
                "operation": operation,
                "status": status,
            },
        )
        return payment


def _view(payment: PaymentOperation) -> dict[str, Any]:
    return {
        "id": payment.id,
        "booking_id": payment.booking_id,
        "operation": payment.operation,
        "status": _response_status(payment.operation, payment.status),
        "amount": payment.amount,
        "currency": payment.currency,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "payment", "status": "ok"}


@app.post("/payments/{operation}", response_model=PaymentView)
def payment_operation(
    operation: str,
    request: PaymentRequest,
    idempotency_key: str = Header(min_length=8, max_length=160),
    session: Session = Depends(_session),
) -> dict[str, Any]:
    if operation not in {"authorize", "capture", "void", "refund"}:
        raise HTTPException(status_code=404, detail="Unknown payment operation")
    return _view(_perform(operation, request, idempotency_key, session))


@app.get("/payments/operations/{operation_key}", response_model=PaymentView)
def get_payment_operation(
    operation_key: str, session: Session = Depends(_session)
) -> dict[str, Any]:
    payment = session.scalar(
        select(PaymentOperation).where(PaymentOperation.operation_key == operation_key)
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment operation not found")
    return _view(payment)
