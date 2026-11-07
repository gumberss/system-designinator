from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from pydantic import BaseModel, Field

from packages.common.booking import utc_now
from services.ai_gateway.assistant_state import (
    AssistantQuote,
    Itinerary,
    get_tool_context,
)

SEARCH_LIMIT = 10
TOOL_TIMEOUT = 8.0

class FlightQuoteInput(BaseModel):
    offer_id: str = Field(min_length=1, max_length=36)
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    departure_date: date | None = None
    passengers: int = Field(ge=1, le=9)


class HotelQuoteInput(BaseModel):
    hotel_id: str = Field(min_length=1, max_length=36)
    room_type_id: str = Field(min_length=1, max_length=36)
    city_code: str = Field(min_length=3, max_length=3)
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=5)
    guests: int = Field(default=1, ge=1, le=8)


class ActivityQuoteInput(BaseModel):
    schedule_id: str = Field(min_length=1, max_length=36)
    city_code: str = Field(min_length=3, max_length=3)
    activity_date: date | None = None
    participants: int = Field(ge=1, le=12)


class ItineraryItemInput(BaseModel):
    day: int = Field(ge=1, le=60)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    city_code: str | None = Field(default=None, min_length=3, max_length=3)


async def _platform_request(
    service: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> Any:
    context = get_tool_context()
    base_url = os.getenv(service)
    if not base_url:
        raise RuntimeError(f"{service} service URL is not configured")
    headers = {"Authorization": f"Bearer {context.bearer_token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TOOL_TIMEOUT)) as client:
            response = await client.request(
                method,
                f"{base_url.rstrip('/')}/{path.lstrip('/')}",
                params=params,
                json=body,
                headers=headers,
            )
    except httpx.TimeoutException as error:
        raise RuntimeError(f"{service} service timed out") from error
    except httpx.RequestError as error:
        raise RuntimeError(f"{service} service is unavailable") from error
    if response.is_error:
        raise RuntimeError(f"{service} request failed with status {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise RuntimeError(f"{service} returned an invalid response") from error


def _as_naive_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


async def search_travel(
    query: str = Field(default="", max_length=120),
    kind: Literal["destination", "flight", "hotel", "activity"] | None = None,
    city_code: str | None = Field(default=None, min_length=3, max_length=3),
    limit: int = Field(default=5, ge=1, le=SEARCH_LIMIT),
) -> dict[str, Any]:
    """Search the current travel catalog; results are advisory, not inventory holds."""
    params: dict[str, Any] = {"limit": limit}
    if query.strip():
        params["query"] = query.strip()
    if kind:
        params["kind"] = kind
    if city_code:
        params["city_code"] = city_code.upper()
    return await _platform_request("SEARCH_URL", "GET", "/search", params=params)


async def create_itinerary(
    title: str = Field(min_length=1, max_length=160),
    items: list[ItineraryItemInput] = Field(min_length=1, max_length=60),
) -> dict[str, Any]:
    """Save a draft itinerary for the authenticated traveler; this does not book."""
    context = get_tool_context()
    now = utc_now()
    itinerary = Itinerary(
        id=str(uuid.uuid4()),
        user_id=context.user_id,
        title=title.strip(),
        items=[item.model_dump(mode="json") for item in items],
        created_at=now,
        updated_at=now,
    )
    with context.session_factory() as session:
        session.add(itinerary)
        session.commit()
    return {
        "id": itinerary.id,
        "title": itinerary.title,
        "items": itinerary.items,
        "created_at": itinerary.created_at.isoformat(),
    }


def get_itinerary(itinerary_id: str = Field(min_length=1, max_length=36)) -> dict[str, Any]:
    """Get a saved itinerary owned by the authenticated traveler."""
    context = get_tool_context()
    with context.session_factory() as session:
        itinerary = session.get(Itinerary, itinerary_id)
        if itinerary is None or itinerary.user_id != context.user_id:
            raise RuntimeError("Itinerary not found")
        return {
            "id": itinerary.id,
            "title": itinerary.title,
            "items": itinerary.items,
            "created_at": itinerary.created_at.isoformat(),
        }


async def create_quote(
    flight: FlightQuoteInput | None = None,
    hotel: HotelQuoteInput | None = None,
    activity: ActivityQuoteInput | None = None,
) -> dict[str, Any]:
    """Prepare a fresh booking quote; this does not reserve inventory or charge."""
    if flight is None and hotel is None and activity is None:
        raise ValueError("At least one booking item is required")
    body = {
        "flight": flight.model_dump(mode="json") if flight is not None else None,
        "hotel": hotel.model_dump(mode="json") if hotel is not None else None,
        "activity": activity.model_dump(mode="json") if activity is not None else None,
    }
    quote = await _platform_request("BOOKING_URL", "POST", "/quotes", body=body)
    context = get_tool_context()
    quote_id = str(quote["id"])
    now = utc_now()
    record = AssistantQuote(
        id=quote_id,
        user_id=context.user_id,
        fingerprint=str(quote["fingerprint"]),
        total_amount=Decimal(str(quote["total_amount"])),
        currency=str(quote["currency"]),
        expires_at=_as_naive_utc(str(quote["expires_at"])),
        status="pending",
        idempotency_key=f"assistant-{uuid.uuid4()}",
        created_at=now,
    )
    with context.session_factory() as session:
        session.add(record)
        session.commit()
    return {
        "quote_id": quote_id,
        "selection": quote["selection"],
        "fingerprint": quote["fingerprint"],
        "total_amount": quote["total_amount"],
        "currency": quote["currency"],
        "expires_at": quote["expires_at"],
        "status": "awaiting_explicit_user_confirmation",
    }


async def get_booking_status(
    booking_id: str = Field(min_length=1, max_length=36),
) -> dict[str, Any]:
    """Get booking status for the authenticated traveler."""
    return await _platform_request(
        "BOOKING_URL",
        "GET",
        f"/bookings/{booking_id}",
    )


async def start_booking(quote_id: str = Field(min_length=1, max_length=36)) -> dict[str, Any]:
    """Start booking only after the traveler explicitly confirmed this exact quote."""
    context = get_tool_context()
    with context.session_factory() as session:
        quote = session.get(AssistantQuote, quote_id)
        if quote is None or quote.user_id != context.user_id:
            raise RuntimeError("Confirmed quote not found")
        if quote.status == "started" and quote.booking_result is not None:
            return quote.booking_result
        if quote.status not in {"confirmed", "starting"}:
            raise RuntimeError("Explicit user confirmation is required")
        if quote.status == "confirmed" and quote.expires_at <= utc_now():
            quote.status = "expired"
            session.commit()
            raise RuntimeError("Quote has expired; prepare a new quote")
        quote.status = "starting"
        session.commit()
        idempotency_key = quote.idempotency_key

    booking = await _platform_request(
        "BOOKING_URL",
        "POST",
        "/bookings",
        body={"quote_id": quote_id},
        idempotency_key=idempotency_key,
    )
    with context.session_factory() as session:
        quote = session.get(AssistantQuote, quote_id)
        if quote is None or quote.user_id != context.user_id:
            raise RuntimeError("Confirmed quote not found")
        quote.status = "started"
        quote.booking_id = str(booking["id"])
        quote.booking_result = booking
        session.commit()
    return booking


def create_mcp_server() -> MCPServer:
    server = MCPServer("AI Travel Marketplace")
    for tool in (
        search_travel,
        create_itinerary,
        get_itinerary,
        create_quote,
        get_booking_status,
        start_booking,
    ):
        server.tool()(tool)
    return server
