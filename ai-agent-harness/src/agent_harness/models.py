import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from agent_harness.async_utils import OperationCancelled, run_interruptible
from agent_harness.budget import estimate_context
from agent_harness.domain import (
    Agent, Execution, HarnessError, ModelAttempt, ModelRequest, ModelResponse, ModelSelection,
    ProviderError, Usage, account_usage, execution_deadline,
)
from agent_harness.ports import CheckpointWriter, ModelProvider
from agent_harness.security import validate_result_schema
from agent_harness.telemetry import telemetry
from agent_harness.resilience import (
    TRANSIENT_PROVIDER_ERRORS, CircuitBreaker, CircuitOpen, LocalCircuitBreaker,
    abandon_probe, retry_delay,
)


class ModelExecutor:
    def __init__(
        self, providers: Mapping[str, ModelProvider], *, breaker: CircuitBreaker | None = None,
        dependency_scopes: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        authorization: Callable[[Execution, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.breaker = breaker if breaker is not None else LocalCircuitBreaker()
        self.scopes = dict(dependency_scopes or {})
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.authorization = authorization

    @staticmethod
    def compatible(selection: ModelSelection, request: ModelRequest) -> bool:
        caps = selection.capabilities
        if caps is None:
            return True
        needs_tools = bool(request.tools or any(
            message.tool_calls or message.role == "tool" for message in request.messages
        ))
        return (
            caps.structured_output and (not needs_tools or caps.tools)
            and caps.context_window_tokens >= estimate_context(request)
            and caps.max_output_tokens >= request.max_output_tokens
        )

    async def generate(
        self, agent: Agent, execution: Execution, request: ModelRequest,
        cancellation: asyncio.Event, writer: CheckpointWriter | None = None,
        *, purpose: Literal["step", "summary"] = "step",
    ) -> tuple[ModelResponse, ModelSelection]:
        if agent.result_schema is not None:
            try:
                validate_result_schema(agent.result_schema)
            except ValueError:
                raise HarnessError("invalid_output_policy", "The configured output policy is invalid.") from None
        with telemetry().operation("step" if purpose == "step" else "context", execution=execution):
            return await self._generate(agent, execution, request, cancellation, writer, purpose=purpose)

    async def _generate(
        self, agent: Agent, execution: Execution, request: ModelRequest,
        cancellation: asyncio.Event, writer: CheckpointWriter | None = None,
        *, purpose: Literal["step", "summary"] = "step",
    ) -> tuple[ModelResponse, ModelSelection]:
        for previous in execution.model_call_records:
            if previous.status == "running":
                previous.status, previous.error_code = "interrupted", "interrupted"
        until = min(
            execution_deadline(execution, agent).timestamp(),
            self.clock() + agent.limits.call_timeout_seconds,
        )

        async def save() -> None:
            if writer is not None:
                await writer.save(execution)

        last: HarnessError = ProviderError("provider_unavailable", "No model route succeeded.")
        for selection in (agent.selection, *agent.fallbacks):
            provider = self.providers.get(selection.provider)
            if provider is None or not self.compatible(selection, request):
                last = HarnessError(
                    "provider_not_configured" if provider is None else "fallback_incompatible",
                    "Model route is not configured or cannot satisfy request capabilities.",
                )
                execution.model_call_records.append(ModelAttempt(
                    selection, purpose, execution.model_attempts, status="blocked",
                    error_code=last.code, finished_at=datetime.now(UTC),
                ))
                await save()
                if selection == agent.selection:
                    raise last
                continue
            for number in range(1, agent.retry.max_attempts + 1):
                if cancellation.is_set():
                    raise OperationCancelled()
                remaining = until - self.clock()
                if remaining <= 0:
                    if self.clock() >= execution_deadline(execution, agent).timestamp():
                        raise TimeoutError
                    raise last
                if execution.model_calls >= agent.limits.max_model_calls:
                    raise HarnessError("model_call_limit", "Maximum physical model calls reached.")
                try:
                    permit = await run_interruptible(
                        lambda: self.breaker.acquire(
                            "provider:" + self.scopes.get(selection.provider, selection.provider)
                        ),
                        cancellation, remaining,
                        HarnessError("coordination_timeout", "Circuit coordination timed out."),
                    )
                except CircuitOpen as exc:
                    last = exc
                    execution.model_call_records.append(ModelAttempt(
                        selection, purpose, execution.model_attempts, status="blocked",
                        error_code=exc.code, finished_at=datetime.now(UTC),
                    ))
                    await save()
                    break
                record = ModelAttempt(selection, purpose, execution.model_attempts)
                execution.model_calls += 1
                execution.model_call_records.append(record)
                execution.unaccounted_calls += 1
                execution.usage_complete = False
                saved = False
                try:
                    await save()
                    saved = True
                finally:
                    if not saved:
                        await abandon_probe(
                            self.breaker, permit, execution_deadline(execution, agent).timestamp() - self.clock(),
                        )
                failure: ProviderError | None = None
                try:
                    if execution.managed:
                        try:
                            if self.authorization is None:
                                raise HarnessError("secure_host_required", "Managed execution requires current authorization.")
                            await self.authorization(execution, None)
                        except (HarnessError, OperationCancelled, asyncio.CancelledError) as exc:
                            record.status = "blocked"
                            record.error_code = exc.code if isinstance(exc, HarnessError) else "cancelled"
                            record.usage = Usage(input_tokens=0, output_tokens=0)
                            execution.unaccounted_calls -= 1
                            execution.usage_complete = execution.unaccounted_calls == 0
                            try:
                                await save()
                            finally:
                                await abandon_probe(
                                    self.breaker, permit, execution_deadline(execution, agent).timestamp() - self.clock(),
                                )
                            raise
                    response = await telemetry().model_call(
                        lambda: run_interruptible(
                            lambda: provider.generate(replace(request, model=selection.model)),
                            cancellation, min(agent.retry.attempt_timeout_seconds, until - self.clock()),
                            ProviderError("provider_timeout", "Model attempt timed out."),
                        ),
                        execution, selection, attempt_id=str(record.id),
                    )
                    record.status = "succeeded"
                    record.usage, record.request_id = response.usage, response.request_id
                except ProviderError as exc:
                    failure = exc
                    record.status, record.error_code = "failed", exc.code
                    record.usage = exc.usage
                finally:
                    record.finished_at = datetime.now(UTC)
                    if record.status == "running":
                        record.status = "interrupted"
                        record.error_code = "interrupted"
                        await abandon_probe(
                            self.breaker, permit, execution_deadline(execution, agent).timestamp() - self.clock(),
                        )
                if record.usage is not None:
                    record.estimated_cost_usd = account_usage(execution, selection, record.usage)
                    execution.unaccounted_calls -= 1
                    execution.usage_complete = execution.unaccounted_calls == 0
                await save()
                transient = failure is not None and failure.code in TRANSIENT_PROVIDER_ERRORS
                try:
                    await run_interruptible(
                        lambda: self.breaker.finish(
                            permit, "failure" if transient else "success",
                            failure.retry_after_seconds if failure else None,
                        ),
                        cancellation, max(
                            0, min(3, execution_deadline(execution, agent).timestamp() - self.clock()),
                        ),
                        HarnessError("coordination_timeout", "Circuit update timed out."),
                    )
                except (OperationCancelled, asyncio.CancelledError):
                    await abandon_probe(
                        self.breaker, permit, execution_deadline(execution, agent).timestamp() - self.clock(),
                    )
                    raise
                if execution.managed and record.usage is not None and (
                    record.usage.output_tokens > request.max_output_tokens
                    or record.usage.input_tokens + record.usage.output_tokens > agent.limits.context_window_tokens
                ):
                    raise HarnessError("provider_usage_limit", "Provider reported usage beyond the reserved call bounds.")
                if failure is None:
                    return response, selection
                last = failure
                if not transient:
                    raise failure
                if number == agent.retry.max_attempts:
                    break
                delay = retry_delay(agent.retry, number, failure.retry_after_seconds, self.jitter)
                if delay >= until - self.clock():
                    break
                await run_interruptible(
                    lambda: self.sleep(delay), cancellation, until - self.clock(),
                    ProviderError("provider_timeout", "Retry wait exceeded the call deadline."),
                )
        raise last
