from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Limits(Contract):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_model_calls: int = Field(default=256, ge=1, le=1024)
    execution_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    call_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_output_tokens: int = Field(default=1024, ge=1, le=32768)
    max_input_bytes: int = Field(default=65536, ge=1, le=1048576)
    max_response_bytes: int = Field(default=262144, ge=1, le=1048576)
    max_tool_calls: int = Field(default=32, ge=1, le=256)
    max_tool_attempts: int = Field(default=256, ge=1, le=4096)
    max_tool_calls_per_step: int = Field(default=8, ge=1, le=32)
    max_tool_argument_bytes: int = Field(default=16384, ge=1, le=1048576)
    max_tool_result_bytes: int = Field(default=65536, ge=1, le=1048576)
    context_window_tokens: int = Field(default=32768, ge=128, le=1000000)


class RetryPolicy(Contract):
    max_attempts: int = Field(default=3, ge=1, le=5)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=60)
    max_delay_seconds: float = Field(default=10, ge=0, le=300)
    attempt_timeout_seconds: float = Field(default=10, gt=0, le=300)


class ModelCapabilities(Contract):
    tools: bool
    structured_output: bool
    context_window_tokens: int = Field(ge=128, le=1000000)
    max_output_tokens: int = Field(ge=1, le=32768)
    residency: str = Field(min_length=1, max_length=100)
    policy_tags: frozenset[str] = frozenset()


class ModelSelection(Contract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    capabilities: ModelCapabilities | None = None


class ApprovalPolicy(Contract):
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    required_tools: frozenset[str] = frozenset()
    require_side_effects: bool = False


class Agent(Contract):
    id: UUID
    tenant_id: UUID
    version: int = Field(default=1, ge=1)
    instructions: str = Field(min_length=1, max_length=16384)
    selection: ModelSelection
    limits: Limits = Field(default_factory=Limits)
    allowed_tools: frozenset[str] = frozenset()
    retry: RetryPolicy = Field(default_factory=lambda: RetryPolicy(
        max_attempts=1, attempt_timeout_seconds=300,
    ))
    fallbacks: tuple[ModelSelection, ...] = Field(default=(), max_length=3)
    result_schema: dict[str, object] | None = None
    approval_policy: ApprovalPolicy | None = None

    @model_validator(mode="after")
    def compatible_fallbacks(self) -> "Agent":
        if self.approval_policy is not None and not self.approval_policy.required_tools <= self.allowed_tools:
            raise ValueError("Approval policy tools must be allowed by the agent.")
        if self.fallbacks:
            primary = self.selection.capabilities
            if primary is None:
                raise ValueError("Fallback requires explicit primary model capabilities.")
            seen = {(self.selection.provider, self.selection.model)}
            for candidate in self.fallbacks:
                caps = candidate.capabilities
                key = (candidate.provider, candidate.model)
                if key in seen or caps is None:
                    raise ValueError("Fallbacks require unique models and explicit capabilities.")
                if caps.residency != primary.residency or not primary.policy_tags <= caps.policy_tags:
                    raise ValueError("Fallback must preserve residency and required policy tags.")
                seen.add(key)
        return self


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    subject: str
    allowed_agent_ids: frozenset[UUID]
    allowed_tools: frozenset[str] = frozenset()


class ToolCall(Contract):
    id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    arguments: str = Field(max_length=1048576)


class ToolDefinition(Contract):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    version: int = Field(default=1, ge=1)
    description: str = Field(min_length=1, max_length=2048)
    timeout_seconds: float = Field(default=10, gt=0, le=300)
    side_effects: bool = True
    supports_idempotency: bool = False
    requires_approval: bool = False
    dependency_key: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    retry: RetryPolicy = Field(default_factory=lambda: RetryPolicy(
        max_attempts=1, attempt_timeout_seconds=300,
    ))

    @model_validator(mode="after")
    def safe_retries(self) -> "ToolDefinition":
        if self.side_effects and self.retry.max_attempts != 1:
            raise ValueError("Automatic retries are restricted to explicitly read-only tools.")
        return self


@dataclass(frozen=True)
class ToolDescriptor:
    definition: ToolDefinition
    input_schema: dict[str, object]
    output_schema: dict[str, object]


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


class Decision(Contract):
    status: Literal["continue", "completed"]
    content: str = Field(min_length=1)


class Usage(Contract):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)


@dataclass(frozen=True)
class ModelRequest:
    model: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    max_response_bytes: int
    tools: tuple[ToolDescriptor, ...] = ()
    max_tool_calls: int = 8


@dataclass(frozen=True)
class ModelResponse:
    decision: Decision | None
    usage: Usage
    request_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    assistant_content: str = ""

    def __post_init__(self) -> None:
        if (self.decision is None) == (not self.tool_calls):
            raise ValueError("A model response must contain either a decision or tool calls.")


class Status(StrEnum):
    WAITING_APPROVAL = "waiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Failure:
    code: str
    message: str


@dataclass(frozen=True)
class Step:
    index: int
    response: ModelResponse
    latency_seconds: float
    estimated_cost_usd: Decimal


@dataclass(frozen=True)
class ToolContext:
    tenant_id: UUID
    subject: str
    execution_id: UUID
    session_id: UUID
    invocation_id: UUID
    deadline: datetime
    idempotency_key: str | None


@dataclass
class ToolAttempt:
    index: int
    status: Literal["running", "succeeded", "failed", "cancelled", "outcome_unknown"] = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    error_code: str | None = None
    dispatch_started: bool | None = None


@dataclass
class ToolInvocation:
    call_id: str
    name: str
    version: int | None = None
    id: UUID = field(default_factory=uuid4)
    arguments_sha256: str | None = None
    status: Literal[
        "prepared", "running", "succeeded", "rejected", "failed", "cancelled", "outcome_unknown"
    ] = "prepared"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    latency_seconds: float = 0
    result: str | None = None
    failure: Failure | None = None
    replay_count: int = 0
    side_effects: bool = True
    supports_idempotency: bool = False
    attempts: int = 0
    attempt_records: list[ToolAttempt] = field(default_factory=list)


@dataclass
class ModelAttempt:
    selection: ModelSelection
    purpose: Literal["step", "summary"]
    step: int
    id: UUID = field(default_factory=uuid4)
    status: Literal["running", "succeeded", "failed", "interrupted", "blocked"] = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    usage: Usage | None = None
    estimated_cost_usd: Decimal = Decimal("0")
    error_code: str | None = None
    request_id: str | None = None


@dataclass
class ApprovalRequest:
    execution_id: UUID
    agent_id: UUID
    agent_version: int
    invocation_id: UUID
    call_id: str
    tool_name: str
    tool_version: int
    arguments: str
    arguments_sha256: str
    definition_sha256: str
    requested_by: str
    expires_at: datetime
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: Literal["pending", "approved", "rejected", "consumed", "expired", "cancelled"] = "pending"
    version: int = 1
    decision: Literal["approve", "reject"] | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None


@dataclass
class Execution:
    tenant_id: UUID
    subject: str
    agent_id: UUID
    agent_version: int
    session_id: UUID
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    status: Status = Status.QUEUED
    steps: list[Step] = field(default_factory=list)
    result: str | None = None
    failure: Failure | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: Decimal = Decimal("0")
    usage_complete: bool = True
    tool_invocations: list[ToolInvocation] = field(default_factory=list)
    tool_call_attempts: int = 0
    tool_dispatches: int = 0
    messages: list[Message] = field(default_factory=list)
    pending_tool_calls: tuple[ToolCall, ...] = ()
    model_attempts: int = 0
    unaccounted_calls: int = 0
    context_tokens_estimate: int = 0
    deadline_at: datetime | None = None
    model_calls: int = 0
    model_call_records: list[ModelAttempt] = field(default_factory=list)
    traceparent: str | None = None
    managed: bool = False
    approvals: list[ApprovalRequest] = field(default_factory=list)


class HarnessError(Exception):
    def __init__(
        self, code: str, message: str, *, usage: Usage | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.usage = usage


class AuthorizationError(HarnessError):
    def __init__(self) -> None:
        super().__init__("access_denied", "Agent execution is not authorized.")


class ApprovalPending(HarnessError):
    def __init__(self) -> None:
        super().__init__("approval_pending", "Execution is waiting for human approval.")


class SessionConflict(HarnessError):
    def __init__(self) -> None:
        super().__init__("session_busy", "This session already has an active execution.")


class ProviderError(HarnessError):
    def __init__(
        self, code: str, message: str, *, usage: Usage | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code, message, usage=usage)
        self.retry_after_seconds = retry_after_seconds


def estimate_cost(selection: ModelSelection, usage: Usage) -> Decimal:
    return (
        Decimal(usage.input_tokens) * selection.input_usd_per_million
        + Decimal(usage.output_tokens) * selection.output_usd_per_million
    ) / Decimal(1_000_000)


def execution_deadline(execution: Execution, agent: Agent) -> datetime:
    limit = execution.created_at + timedelta(seconds=agent.limits.execution_timeout_seconds)
    return min(limit, execution.deadline_at) if execution.deadline_at is not None else limit


def account_usage(execution: Execution, selection: ModelSelection, usage: Usage) -> Decimal:
    cost = estimate_cost(selection, usage)
    execution.input_tokens += usage.input_tokens
    execution.output_tokens += usage.output_tokens
    execution.estimated_cost_usd += cost
    return cost
