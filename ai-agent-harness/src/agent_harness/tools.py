import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from agent_harness.async_utils import OperationCancelled, run_interruptible
from agent_harness.approvals import ApprovalAuthorizer, request_for, requires_approval
from agent_harness.domain import (
    Agent,
    ApprovalPending,
    Contract,
    Execution,
    Failure,
    HarnessError,
    Principal,
    Status,
    ToolCall,
    ToolContext,
    ToolDefinition,
    ToolDescriptor,
    ToolInvocation,
    ToolAttempt,
    execution_deadline,
)
from agent_harness.json_codec import decode_object
from agent_harness.telemetry import telemetry
from agent_harness.resilience import (
    CircuitBreaker, LocalCircuitBreaker, Outcome, Permit, abandon_probe, retry_delay,
)


I = TypeVar("I", bound=Contract)
O = TypeVar("O", bound=Contract)
ToolOperation = Callable[[ToolContext], Awaitable[str]]


class InstalledTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def describe(self) -> ToolDescriptor: ...

    def prepare(self, arguments: dict[str, object]) -> ToolOperation: ...


@dataclass(frozen=True)
class TypedTool(Generic[I, O]):
    definition: ToolDefinition
    input_type: type[I]
    output_type: type[O]
    handler: Callable[[I, ToolContext], Awaitable[O]]

    def __post_init__(self) -> None:
        if any(
            model.model_config.get("extra") != "forbid"
            for model in (self.input_type, self.output_type)
        ):
            raise ValueError("Tool schemas must forbid extra fields.")

    def describe(self) -> ToolDescriptor:
        return ToolDescriptor(
            self.definition,
            self.input_type.model_json_schema(),
            self.output_type.model_json_schema(),
        )

    def prepare(self, arguments: dict[str, object]) -> ToolOperation:
        try:
            validated = self.input_type.model_validate(arguments, strict=True)
        except ValidationError:
            raise HarnessError(
                "tool_invalid_arguments", "Tool arguments violate the input schema."
            ) from None

        async def invoke(context: ToolContext) -> str:
            result = await self.handler(validated, context)
            try:
                checked = self.output_type.model_validate(
                    result.model_dump(mode="python", warnings="error"), strict=True
                )
                return checked.model_dump_json(warnings="error")
            except (ValidationError, PydanticSerializationError):
                raise HarnessError(
                    "tool_invalid_result", "Tool output violates the result schema."
                ) from None

        return invoke


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, InstalledTool] = {}
        self._disabled: set[str] = set()
        self._grants: dict[UUID, set[str]] = {}

    def register(self, tool: InstalledTool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise HarnessError("tool_already_registered", "Tool names cannot be overwritten.")
        self._tools[name] = tool

    def resolve(self, name: str) -> InstalledTool:
        tool = self._tools.get(name)
        if tool is None:
            raise HarnessError("tool_not_found", "Tool implementation is not installed.")
        return tool

    def list_installed(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._tools[name].describe() for name in sorted(self._tools))

    def set_enabled(self, name: str, enabled: bool) -> None:
        self.resolve(name)
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def set_tenant_grant(self, tenant_id: UUID, name: str, allowed: bool) -> None:
        self.resolve(name)
        grants = self._grants.setdefault(tenant_id, set())
        if allowed:
            grants.add(name)
        else:
            grants.discard(name)

    def _permitted(self, name: str, agent: Agent, principal: Principal) -> bool:
        return (
            principal.tenant_id == agent.tenant_id
            and agent.id in principal.allowed_agent_ids
            and bool(principal.subject.strip())
            and name in agent.allowed_tools
            and name in principal.allowed_tools
            and name in self._grants.get(principal.tenant_id, set())
            and name not in self._disabled
        )

    def authorize(
        self, name: str, agent: Agent, principal: Principal, *, allow_approval: bool = False,
    ) -> InstalledTool:
        if not self._permitted(name, agent, principal):
            raise HarnessError("tool_access_denied", "Tool invocation is not authorized.")
        tool = self.resolve(name)
        if tool.definition.requires_approval and not allow_approval:
            raise HarnessError("approval_required", "This tool requires a human approval workflow.")
        return tool

    def discover(
        self, agent: Agent, principal: Principal, *, include_approval: bool = False,
    ) -> tuple[ToolDescriptor, ...]:
        visible: list[ToolDescriptor] = []
        for name in sorted(self._tools):
            if (
                self._permitted(name, agent, principal)
                and (include_approval or not self._tools[name].definition.requires_approval)
            ):
                visible.append(self._tools[name].describe())
        return tuple(visible)


@dataclass(frozen=True)
class PreparedInvocation:
    record: ToolInvocation
    tool: InstalledTool
    operation: ToolOperation


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, *, breaker: CircuitBreaker | None = None,
        authorization: Callable[[Execution, str | None], Awaitable[None]] | None = None,
        approvals_enabled: bool = False,
    ) -> None:
        self.registry = registry
        self.breaker = breaker if breaker is not None else LocalCircuitBreaker()
        self.authorization = authorization
        self.approvals_enabled = approvals_enabled

    async def invoke_batch(
        self,
        calls: tuple[ToolCall, ...],
        agent: Agent,
        principal: Principal,
        execution: Execution,
        cancellation: asyncio.Event,
        offered: tuple[ToolDescriptor, ...],
        *,
        save: Callable[[], Awaitable[None]] | None = None,
        recover: bool = False,
        approval_authorizer: ApprovalAuthorizer | None = None,
    ) -> tuple[ToolInvocation, ...]:
        self._check_scope(agent, principal, execution)
        execution.tool_dispatches = max(
            execution.tool_dispatches,
            sum(record.attempts or int(record.started_at is not None) for record in execution.tool_invocations),
        )
        if not calls:
            raise HarnessError("tool_invalid_batch", "At least one tool call is required.")
        if (
            len(calls) > agent.limits.max_tool_calls_per_step
            or (not recover and execution.tool_call_attempts + len(calls) > agent.limits.max_tool_calls)
        ):
            raise HarnessError("tool_call_limit", "Tool invocation limit exceeded.")
        if not recover:
            execution.tool_call_attempts += len(calls)
        if len({call.id for call in calls}) != len(calls):
            raise HarnessError("duplicate_tool_call_id", "Tool call IDs must be unique in a batch.")
        prepared: list[PreparedInvocation] = []
        results: list[ToolInvocation] = []
        waiting = False
        try:
            for call in calls:
                record, item = self._prepare(call, agent, principal, execution, offered, recover)
                results.append(record)
                if item is not None:
                    prepared.append(item)
            pending = False
            for item in prepared:
                if requires_approval(agent, item.tool.definition):
                    if not self.approvals_enabled or approval_authorizer is None or save is None:
                        raise HarnessError("approval_required", "This tool requires a durable approval host.")
                    call = next(call for call in calls if call.id == item.record.call_id)
                    approval = request_for(execution, agent, item.record, call, item.tool.describe())
                    pending = pending or approval.status == "pending"
            if pending:
                waiting = True
                raise ApprovalPending()
            if save:
                await save()
            for item in prepared:
                await self._invoke(item, agent, principal, execution, cancellation, save, approval_authorizer)
        finally:
            for item in prepared:
                if item.record.status == "prepared" and not waiting:
                    item.record.status = "cancelled"
                    item.record.failure = Failure("not_dispatched", "Tool invocation was not dispatched.")
                    item.record.finished_at = datetime.now(UTC)
            if save and not waiting:
                await save()
        return tuple(results)

    @staticmethod
    def _check_scope(agent: Agent, principal: Principal, execution: Execution) -> None:
        if (
            execution.tenant_id != principal.tenant_id
            or execution.tenant_id != agent.tenant_id
            or execution.subject != principal.subject
            or execution.agent_id != agent.id
            or execution.agent_version != agent.version
            or execution.status != Status.RUNNING
        ):
            raise HarnessError("tool_access_denied", "Execution scope is not authorized.")

    def _prepare(
        self,
        call: ToolCall,
        agent: Agent,
        principal: Principal,
        execution: Execution,
        offered: tuple[ToolDescriptor, ...],
        recover: bool,
    ) -> tuple[ToolInvocation, PreparedInvocation | None]:
        record = ToolInvocation(call.id, call.name)
        try:
            tool = self.registry.authorize(call.name, agent, principal, allow_approval=self.approvals_enabled)
            record.version = tool.definition.version
            record.side_effects = tool.definition.side_effects
            record.supports_idempotency = tool.definition.supports_idempotency
            if not any(
                descriptor.definition == tool.definition for descriptor in offered
            ):
                raise HarnessError("tool_not_offered", "Tool was not offered in this model step.")
            try:
                if len(call.arguments.encode("utf-8")) > agent.limits.max_tool_argument_bytes:
                    raise HarnessError("tool_arguments_too_large", "Tool arguments exceed the byte limit.")
                arguments = decode_object(call.arguments)
                canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except ValueError:
                raise HarnessError("tool_invalid_arguments", "Tool arguments must be a valid JSON object.") from None
            record.arguments_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            operation = tool.prepare(arguments)
            previous = next(
                (item for item in execution.tool_invocations if item.call_id == call.id), None
            )
            if previous is not None:
                if (
                    previous.name != call.name
                    or previous.version != record.version
                    or previous.arguments_sha256 != record.arguments_sha256
                ):
                    raise HarnessError("tool_idempotency_conflict", "Tool call ID was reused with different input.")
                if previous.status == "succeeded":
                    previous.replay_count += 1
                    return previous, None
                if recover and previous.status in ("prepared", "running"):
                    if previous.status == "running" and previous.side_effects and not previous.supports_idempotency:
                        previous.status = "outcome_unknown"
                        previous.failure = Failure("tool_outcome_unknown", "External effects require reconciliation.")
                        raise HarnessError("tool_outcome_unknown", "External effects require reconciliation.")
                    previous.status = "prepared"
                    return previous, PreparedInvocation(previous, tool, operation)
                raise HarnessError("tool_replay_blocked", "This tool call cannot be dispatched again.")
        except HarnessError as exc:
            record.status = "rejected"
            record.failure = Failure(exc.code, exc.message)
            record.finished_at = datetime.now(UTC)
            execution.tool_invocations.append(record)
            raise
        execution.tool_invocations.append(record)
        return record, PreparedInvocation(record, tool, operation)

    async def _invoke(
        self,
        item: PreparedInvocation,
        agent: Agent,
        principal: Principal,
        execution: Execution,
        cancellation: asyncio.Event,
        save: Callable[[], Awaitable[None]] | None,
        approval_authorizer: ApprovalAuthorizer | None,
    ) -> None:
        with telemetry().operation(
            "tool_invocation", execution=execution, resource=item.record.name,
        ) as observation:
            observation.span.set_attribute("harness.tool.invocation.id", str(item.record.id))
            await self._invoke_observed(item, agent, principal, execution, cancellation, save, approval_authorizer)
            if item.record.failure:
                observation.fail(item.record.failure.code)

    async def _invoke_observed(
        self,
        item: PreparedInvocation,
        agent: Agent,
        principal: Principal,
        execution: Execution,
        cancellation: asyncio.Event,
        save: Callable[[], Awaitable[None]] | None,
        approval_authorizer: ApprovalAuthorizer | None,
    ) -> None:
        record = item.record
        definition = item.tool.definition
        loop = asyncio.get_running_loop()
        started = loop.time()
        permit: Permit | None = None
        dispatched = False
        try:
            self.registry.authorize(record.name, agent, principal, allow_approval=self.approvals_enabled)
            context = ToolContext(
                execution.tenant_id,
                execution.subject,
                execution.id,
                execution.session_id,
                record.id,
                min(
                    execution_deadline(execution, agent),
                    datetime.now(UTC) + timedelta(seconds=definition.timeout_seconds),
                ),
                hashlib.sha256(
                    f"{execution.tenant_id}:{execution.id}:{record.id}".encode("ascii")
                ).hexdigest() if definition.supports_idempotency else None,
            )
            remaining = (context.deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise HarnessError("deadline_exceeded", "Execution deadline exceeded before tool dispatch.")

            async def finish(outcome: Outcome) -> None:
                if permit is None:
                    raise TypeError("Tool dispatch requires a circuit permit.")
                active_permit = permit
                await run_interruptible(
                    lambda: self.breaker.finish(
                        active_permit, outcome,
                    ), cancellation,
                    max(0, min(3, (execution_deadline(execution, agent) - datetime.now(UTC)).total_seconds())),
                    HarnessError("coordination_timeout", "Tool circuit update timed out."),
                )

            async def dispatch() -> str:
                nonlocal dispatched
                dispatched = False
                self.registry.authorize(record.name, agent, principal, allow_approval=self.approvals_enabled)
                if execution.tool_dispatches >= agent.limits.max_tool_attempts:
                    raise HarnessError("tool_attempt_limit", "Maximum physical tool dispatches reached.")
                record.status = "running"
                record.started_at = datetime.now(UTC)
                record.attempts += 1
                execution.tool_dispatches += 1
                tool_attempt = ToolAttempt(record.attempts, dispatch_started=False)
                for previous in record.attempt_records:
                    if previous.status == "running":
                        previous.status = "outcome_unknown" if definition.side_effects else "cancelled"
                        previous.error_code = "interrupted"
                record.attempt_records.append(tool_attempt)
                if save:
                    await save()
                if cancellation.is_set():
                    raise OperationCancelled()
                if datetime.now(UTC) >= context.deadline:
                    raise HarnessError("tool_timeout", "Tool deadline expired before callback dispatch.")
                if execution.managed or requires_approval(agent, definition):
                    try:
                        if execution.managed and self.authorization is None:
                            raise HarnessError("secure_host_required", "Managed execution requires current authorization.")
                        if execution.managed and self.authorization is not None:
                            await self.authorization(execution, record.name)
                        if requires_approval(agent, definition):
                            if approval_authorizer is None:
                                raise HarnessError("approval_required", "A durable approval host is required.")
                            await approval_authorizer.consume(execution, record, item.tool.describe())
                        if cancellation.is_set():
                            raise OperationCancelled()
                        if datetime.now(UTC) >= context.deadline:
                            raise HarnessError("tool_timeout", "Tool deadline expired before callback dispatch.")
                    except (HarnessError, OperationCancelled, asyncio.CancelledError) as exc:
                        record.status = "prepared"
                        tool_attempt.status = "failed" if isinstance(exc, HarnessError) else "cancelled"
                        tool_attempt.error_code = exc.code if isinstance(exc, HarnessError) else "cancelled"
                        tool_attempt.finished_at = datetime.now(UTC)
                        raise
                try:
                    tool_attempt.dispatch_started = True
                    if save:
                        await save()
                    if cancellation.is_set():
                        raise OperationCancelled()
                    if datetime.now(UTC) >= context.deadline:
                        raise HarnessError("tool_timeout", "Tool deadline expired before callback dispatch.")
                    dispatched = True
                    with telemetry().operation("tool", execution=execution, resource=record.name):
                        result = await item.operation(context)
                    tool_attempt.status = "succeeded"
                    return result
                except TimeoutError:
                    tool_attempt.status = "outcome_unknown" if definition.side_effects else "failed"
                    tool_attempt.error_code = "tool_timeout"
                    raise HarnessError("tool_timeout", "Tool dependency timed out.") from None
                except HarnessError as exc:
                    tool_attempt.status = "outcome_unknown" if definition.side_effects else "failed"
                    tool_attempt.error_code = exc.code
                    raise
                finally:
                    tool_attempt.finished_at = datetime.now(UTC)
                    if tool_attempt.status == "running":
                        tool_attempt.status = "outcome_unknown" if definition.side_effects else "cancelled"
                        tool_attempt.error_code = "interrupted"

            for attempt in range(1, definition.retry.max_attempts + 1):
                remaining = (context.deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise HarnessError("tool_timeout", "Tool execution exceeded its timeout.")
                permit = await run_interruptible(
                    lambda: self.breaker.acquire(
                        f"tool:{execution.tenant_id}:"
                        + (definition.dependency_key or f"{record.name}:{definition.version}")
                    ), cancellation, remaining,
                    HarnessError("coordination_timeout", "Tool circuit coordination timed out."),
                )
                transient = False
                previous_attempts = record.attempts
                try:
                    result = await run_interruptible(
                        dispatch, cancellation, min(
                            definition.retry.attempt_timeout_seconds,
                            (context.deadline - datetime.now(UTC)).total_seconds(),
                        ),
                        HarnessError("tool_timeout", "Tool execution exceeded its timeout."),
                    )
                except HarnessError as exc:
                    transient = exc.code in {"tool_timeout", "tool_unavailable"}
                    if save:
                        await save()
                    await finish(
                        "failure" if transient else (
                            "success" if record.attempts > previous_attempts else "neutral"
                        ),
                    )
                    if not transient or definition.side_effects or attempt == definition.retry.max_attempts:
                        raise
                    delay = retry_delay(definition.retry, attempt)
                    remaining = (context.deadline - datetime.now(UTC)).total_seconds()
                    if delay >= remaining:
                        raise
                    await run_interruptible(
                        lambda: asyncio.sleep(delay), cancellation, remaining,
                        HarnessError("tool_timeout", "Tool retry exceeded its deadline."),
                    )
                else:
                    break
            if len(result.encode("utf-8")) > agent.limits.max_tool_result_bytes:
                raise HarnessError("tool_result_too_large", "Tool result exceeds the byte limit.")
            record.result = result
            record.status = "succeeded"
            if save:
                await save()
            await finish("success")
        except (OperationCancelled, asyncio.CancelledError):
            if permit is not None:
                await abandon_probe(
                    self.breaker, permit,
                    (execution_deadline(execution, agent) - datetime.now(UTC)).total_seconds(),
                )
            if record.status == "succeeded":
                raise
            if record.status == "running" and definition.side_effects and dispatched:
                record.status = "outcome_unknown"
                record.failure = Failure("tool_outcome_unknown", "External effects require reconciliation.")
            else:
                record.status = "cancelled"
                record.failure = Failure("cancelled", "Tool invocation was cancelled.")
            raise
        except HarnessError as exc:
            if record.status == "succeeded":
                raise
            record.failure = Failure(exc.code, exc.message)
            if record.status == "running" and definition.side_effects and dispatched:
                record.status = "outcome_unknown"
                raise HarnessError("tool_outcome_unknown", "External effects require reconciliation.") from None
            record.status = "failed"
            raise
        finally:
            for attempt_record in record.attempt_records:
                if attempt_record.status == "running":
                    attempt_record.status = "outcome_unknown" if definition.side_effects and dispatched else "cancelled"
                    attempt_record.error_code = "interrupted"
                    attempt_record.finished_at = datetime.now(UTC)
            if record.status == "running":
                record.status = "outcome_unknown" if definition.side_effects and dispatched else "failed"
                record.failure = Failure("tool_internal_error", "Unexpected tool implementation failure.")
            record.finished_at = datetime.now(UTC)
            record.latency_seconds = loop.time() - started
            if save:
                await save()
