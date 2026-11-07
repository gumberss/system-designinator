from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, JSON, Numeric, String, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from packages.common.booking import utc_now


CONVERSATION_RETENTION = timedelta(days=90)


class AssistantBase(DeclarativeBase):
    pass


class Conversation(AssistantBase):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    messages: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)


def purge_expired_conversations(
    factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
) -> int:
    cutoff = (now or utc_now()) - CONVERSATION_RETENTION
    with factory.begin() as session:
        result = session.execute(
            delete(Conversation).where(Conversation.updated_at <= cutoff)
        )
    return int(result.rowcount or 0)


class Itinerary(AssistantBase):
    __tablename__ = "assistant_itineraries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(160))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class AssistantQuote(AssistantBase):
    __tablename__ = "assistant_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    booking_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    booking_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    bearer_token: str
    session_factory: sessionmaker[Session]


_tool_context: ContextVar[ToolContext | None] = ContextVar(
    "assistant_tool_context", default=None
)


def set_tool_context(context: ToolContext) -> Any:
    return _tool_context.set(context)


def reset_tool_context(token: Any) -> None:
    _tool_context.reset(token)


def get_tool_context() -> ToolContext:
    context = _tool_context.get()
    if context is None:
        raise RuntimeError("Authenticated assistant context is required")
    return context
