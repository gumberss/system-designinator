from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from agent_harness.domain import Agent, Execution, Message, Principal


@dataclass
class Envelope:
    agent: Agent
    principal: Principal
    user_input: str
    execution: Execution
    schema_version: Literal[1] = 1


@dataclass(frozen=True)
class HistoryMessage:
    seq: int
    message: Message


@dataclass(frozen=True)
class Summary:
    through_seq: int
    content: str
    version: int
    provider: str = ""
    model: str = ""
    source_start_seq: int = 0


@dataclass(frozen=True)
class SessionContext:
    history: tuple[HistoryMessage, ...]
    summary: Summary | None
    memories: tuple[str, ...]


@dataclass
class Lease:
    tenant_id: UUID
    execution_id: UUID
    worker_id: UUID
    token: int
    version: int
    envelope: Envelope
    cancel_requested: bool


class LeaseLost(Exception):
    """A stale worker must stop without publishing a terminal checkpoint."""


ENVELOPE = TypeAdapter(Envelope)
MESSAGE = TypeAdapter(Message)
SUMMARY = TypeAdapter(Summary)
