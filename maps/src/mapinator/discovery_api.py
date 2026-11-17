from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import ConfigDict, Field, field_validator, model_validator

from mapinator.discovery import (
    Center,
    Criteria,
    DiscoveredPlace,
    DiscoverySchema,
    LocalInterpreter,
    merge_criteria,
    retrieve,
    utc_now,
)
from mapinator.discovery_store import ConversationStore, require_revision
from mapinator.domain import DomainError, Identity, Repository


class DiscoveryRequestSchema(DiscoverySchema):
    # HTTP JSON must parse UUIDs/datetimes; the model adapter stays strictly typed.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=False)


class PlaceSearchRequest(DiscoveryRequestSchema):
    center: Center
    criteria: Criteria = Field(default_factory=Criteria)
    open_at: datetime | None = None
    limit: int = Field(default=20, ge=1, le=50)
    cursor: int = Field(default=0, ge=0, le=100_000)

    @field_validator("open_at")
    @classmethod
    def aware_instant(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("open_at must include a timezone.")
        return value

    @model_validator(mode="after")
    def unambiguous_time(self) -> "PlaceSearchRequest":
        if self.open_at is not None and self.criteria.open_now:
            raise ValueError("Choose open_at or open_now, not both.")
        return self


class PlaceSearchResponse(DiscoveryRequestSchema):
    criteria: Criteria
    items: list[DiscoveredPlace]
    next_cursor: int | None
    evaluated_at: datetime
    dataset_id: UUID
    coverage: Literal["active_local_osm_dataset"] = "active_local_osm_dataset"
    warnings: list[str]
    explanation: str
    license: str = "ODbL 1.0; © OpenStreetMap contributors"


class ConversationRequest(DiscoveryRequestSchema):
    message: str = Field(min_length=1, max_length=2000)
    center: Center
    conversation_id: UUID | None = None
    revision: int | None = Field(default=None, ge=1, le=2**63 - 2)
    criteria: Criteria | None = None
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank.")
        return value

    @model_validator(mode="after")
    def valid_follow_up(self) -> "ConversationRequest":
        if (self.conversation_id is None) != (self.revision is None):
            raise ValueError("conversation_id and revision must be supplied together.")
        if self.conversation_id is not None and self.criteria is not None:
            raise ValueError("Follow-up criteria come from the stored revision, not the request.")
        return self


class ConversationResponse(PlaceSearchResponse):
    conversation_id: UUID
    revision: int
    expires_at: datetime
    interpretation_source: Literal["local_ollama"] = "local_ollama"


def _search(
    repo: Repository,
    center: Center,
    criteria: Criteria,
    at: datetime,
    limit: int,
    cursor: int = 0,
    *,
    require_open: bool = False,
) -> PlaceSearchResponse:
    filters = criteria.model_copy(update={"open_now": True}) if require_open else criteria
    items, next_cursor, dataset_id, warnings = retrieve(
        repo, center, filters, at, limit=limit, cursor=cursor
    )
    return PlaceSearchResponse(
        criteria=criteria,
        items=items,
        next_cursor=next_cursor,
        evaluated_at=at,
        dataset_id=dataset_id,
        warnings=warnings,
        explanation=(
            f"{len(items)} places verified against imported OSM category, distance and requested "
            "filters; ordered by distance, name and OSM ID. Unknown facts do not satisfy filters. "
            "Opening status reflects supported OSM schedules, not a live operational guarantee."
        ),
    )


def create_discovery_router(
    repo: Repository,
    mutation_guard: Callable[..., Identity],
    llm: LocalInterpreter | None,
    store: ConversationStore | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/places", tags=["discovery"])

    @router.post("/search", response_model=PlaceSearchResponse)
    def search(payload: PlaceSearchRequest) -> PlaceSearchResponse:
        return _search(
            repo,
            payload.center,
            payload.criteria,
            payload.open_at or utc_now(),
            payload.limit,
            payload.cursor,
            require_open=payload.open_at is not None,
        )

    @router.post("/conversation", response_model=ConversationResponse)
    def conversation(
        payload: ConversationRequest,
        identity: Annotated[Identity, Depends(mutation_guard)],
    ) -> ConversationResponse:
        if llm is None:
            raise DomainError(503, "llm_not_configured", "Configure a real local Ollama model.")
        if store is None:
            raise DomainError(
                503, "conversation_store_unavailable", "Configure a durable conversation store."
            )
        now = utc_now()
        current = None
        if payload.conversation_id is not None:
            current = store.get(identity.owner_id, payload.conversation_id, now)
            if current is None:
                raise DomainError(404, "conversation_not_found", "Conversation unavailable.")
            assert payload.revision is not None
            require_revision(current, payload.revision)
        previous = current.criteria if current else payload.criteria or Criteria()
        criteria = merge_criteria(previous, llm.interpret(payload.message, previous))
        result = _search(repo, payload.center, criteria, utc_now(), payload.limit)
        # Recheck expiry/revision at commit after the potentially slow model/retrieval call.
        commit_at = utc_now()
        if identity.expires_at <= commit_at:
            raise DomainError(401, "authentication_required", "Browser session expired.")
        saved = (
            store.update(
                identity.owner_id, current.conversation_id, current.revision, criteria, commit_at
            )
            if current
            else store.create(identity.owner_id, criteria, commit_at)
        )
        return ConversationResponse(
            **result.model_dump(),
            conversation_id=saved.conversation_id,
            revision=saved.revision,
            expires_at=saved.expires_at,
        )

    return router
