import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine, RowMapping

from mapinator.discovery import Criteria
from mapinator.domain import DomainError


@dataclass(frozen=True)
class Conversation:
    conversation_id: UUID
    owner_id: UUID
    criteria: Criteria
    revision: int
    expires_at: datetime


class ConversationStore(Protocol):
    def create(self, owner_id: UUID, criteria: Criteria, now: datetime) -> Conversation: ...
    def get(self, owner_id: UUID, conversation_id: UUID, now: datetime) -> Conversation | None: ...
    def update(
        self,
        owner_id: UUID,
        conversation_id: UUID,
        revision: int,
        criteria: Criteria,
        now: datetime,
    ) -> Conversation: ...
    def cleanup(self, now: datetime) -> None: ...


def require_revision(current: Conversation, revision: int) -> None:
    if current.revision != revision:
        raise DomainError(409, "conversation_revision_conflict", "Conversation revision changed.")


class PostgresConversationStore:
    def __init__(self, engine: Engine, *, ttl_s: int = 1800) -> None:
        if not 60 <= ttl_s <= 86_400:
            raise ValueError("Conversation TTL must be between 60 seconds and 24 hours.")
        self.engine = engine
        self.ttl_s = ttl_s

    @staticmethod
    def _conversation(row: RowMapping) -> Conversation:
        return Conversation(
            row["conversation_id"],
            row["owner_id"],
            Criteria.model_validate(row["criteria"]),
            row["revision"],
            row["expires_at"],
        )

    def create(self, owner_id: UUID, criteria: Criteria, now: datetime) -> Conversation:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""
                INSERT INTO discovery_conversations
                (conversation_id,owner_id,criteria,revision,created_at,expires_at)
                VALUES (:id,:owner,CAST(:criteria AS jsonb),1,:now,:expiry) RETURNING *
            """),
                    {
                        "id": uuid4(),
                        "owner": owner_id,
                        "criteria": criteria.model_dump_json(),
                        "now": now,
                        "expiry": now + timedelta(seconds=self.ttl_s),
                    },
                )
                .mappings()
                .one()
            )
            return self._conversation(row)

    def get(self, owner_id: UUID, conversation_id: UUID, now: datetime) -> Conversation | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT * FROM discovery_conversations
                WHERE conversation_id=:id AND owner_id=:owner AND expires_at>:now
            """),
                    {"id": conversation_id, "owner": owner_id, "now": now},
                )
                .mappings()
                .one_or_none()
            )
            return self._conversation(row) if row is not None else None

    def update(
        self,
        owner_id: UUID,
        conversation_id: UUID,
        revision: int,
        criteria: Criteria,
        now: datetime,
    ) -> Conversation:
        with self.engine.begin() as connection:
            # The revision comparison remains in the UPDATE even while locked:
            # concurrent processes cannot both commit the same expected revision.
            current = (
                connection.execute(
                    text("""
                SELECT * FROM discovery_conversations
                WHERE conversation_id=:id AND owner_id=:owner AND expires_at>:now FOR UPDATE
            """),
                    {"id": conversation_id, "owner": owner_id, "now": now},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise DomainError(404, "conversation_not_found", "Conversation unavailable.")
            require_revision(self._conversation(current), revision)
            row = (
                connection.execute(
                    text("""
                UPDATE discovery_conversations SET criteria=CAST(:criteria AS jsonb),
                    revision=revision+1,expires_at=:expiry
                WHERE conversation_id=:id AND owner_id=:owner
                    AND revision=:revision AND expires_at>:now RETURNING *
            """),
                    {
                        "id": conversation_id,
                        "owner": owner_id,
                        "revision": revision,
                        "criteria": json.dumps(criteria.model_dump()),
                        "now": now,
                        "expiry": now + timedelta(seconds=self.ttl_s),
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise DomainError(
                    409, "conversation_revision_conflict", "Conversation revision changed."
                )
            return self._conversation(row)

    def cleanup(self, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM discovery_conversations WHERE expires_at<=:now"), {"now": now}
            )
