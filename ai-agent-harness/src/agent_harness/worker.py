import asyncio
import logging
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

from agent_harness.adapters.postgres import PostgresStore, PostgresWriter
from agent_harness.adapters.redis_signals import RedisSignals
from agent_harness.adapters.workflow_postgres import WorkflowStore
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.adapters.approvals_postgres import PostgresApprovalAuthorizer
from agent_harness.async_utils import OperationCancelled
from agent_harness.context import ContextManager
from agent_harness.domain import Execution, Failure, HarnessError, Message, RetryPolicy, Status, execution_deadline
from agent_harness.ports import ModelProvider
from agent_harness.runtime import Runtime, system_messages
from agent_harness.state import Lease, LeaseLost
from agent_harness.resilience import retry_delay
from agent_harness.telemetry import failure_category, telemetry, traceparent


logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self, store: PostgresStore, runtime: Runtime, providers: dict[str, ModelProvider],
        *, signals: RedisSignals | None = None, lease_seconds: float = 30,
        tenant_id: UUID | None = None,
        security: SecurityRepository | None = None,
        managed_only: bool = False,
    ) -> None:
        if not 0 < lease_seconds <= 300:
            raise ValueError("Lease duration must be in (0, 300].")
        self.store, self.runtime, self.providers = store, runtime, dict(providers)
        self.signals, self.lease_seconds = signals, lease_seconds
        self.id = uuid4()
        self.tenant_id = tenant_id
        self.workflows = WorkflowStore(store)
        self._next_queue_observation = 0.0
        self.security = security
        self.managed_only = managed_only

    async def _execute(self, lease: Lease, cancellation: asyncio.Event) -> Execution:
        execution = lease.envelope.execution
        with telemetry().operation(
            "worker", execution=execution, linked_traceparent=execution.traceparent,
        ) as observation:
            execution.traceparent = traceparent() or execution.traceparent
            result = await self._execute_observed(lease, cancellation)
            if result.failure:
                observation.fail(result.failure.code)
            return result

    async def _execute_observed(self, lease: Lease, cancellation: asyncio.Event) -> Execution:
        envelope = lease.envelope
        execution, agent = envelope.execution, envelope.agent
        writer = PostgresWriter(self.store, lease)
        execution.status = Status.RUNNING
        try:
            if execution.managed:
                if self.security is None:
                    raise HarnessError("secure_host_required", "Use the managed worker for API executions.")
                await self.security.authorize_execution(execution)
                grants = await SecurityRepository(self.store.scoped(execution.tenant_id)).tool_grants(execution.tenant_id)
                self.runtime.set_tool_grants(execution.tenant_id, {name: state[0] for name, state in grants.items()})
            history: tuple[Message, ...] = ()
            if not execution.messages and not cancellation.is_set():
                provider = self.providers.get(agent.selection.provider)
                if provider is None:
                    raise HarnessError("provider_not_configured", "The selected provider is not configured.")
                remaining = (
                    execution_deadline(execution, agent) - datetime.now(UTC)
                ).total_seconds()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    context = await self.store.context(envelope.principal, execution.session_id)
                    history, summary = await ContextManager(provider, models=self.runtime.models).prepare(
                        agent, execution, context, envelope.user_input,
                        system_messages(agent),
                        writer, cancellation,
                        self.runtime.describe_tools(agent, envelope.principal),
                    )
                    if summary is not None and summary != context.summary:
                        await self.store.save_summary(lease, summary)
            return await self.runtime.execute(
                agent, envelope.principal, execution.session_id, envelope.user_input,
                resume=execution, history=history, checkpoint=writer, cancellation=cancellation,
                approval_authorizer=PostgresApprovalAuthorizer(self.store, lease) if execution.managed and self.security else None,
            )
        except OperationCancelled:
            execution.status = Status.CANCELLED
        except TimeoutError:
            execution.status = Status.EXPIRED
            execution.failure = Failure("deadline_exceeded", "Execution deadline exceeded during context preparation.")
        except HarnessError as exc:
            execution.status = Status.FAILED
            execution.failure = Failure(exc.code, exc.message)
        execution.finished_at = datetime.now(UTC)
        await writer.save(execution)
        return execution

    async def run_once(self) -> Execution | None:
        now = time.monotonic()
        if now >= self._next_queue_observation:
            await self.store.observe_queue(self.tenant_id)
            self._next_queue_observation = now + 10
        await self.workflows.tick(self.tenant_id, managed_only=self.managed_only)
        lease = await self.store.claim(
            self.id, self.lease_seconds, self.tenant_id, managed_only=self.managed_only,
        )
        if lease is None:
            return None
        cancellation, stop = asyncio.Event(), asyncio.Event()
        if lease.cancel_requested:
            cancellation.set()

        async def maintain() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), self.lease_seconds / 3)
                except TimeoutError:
                    if await self.store.heartbeat(lease, self.lease_seconds):
                        cancellation.set()

        heartbeat = asyncio.create_task(maintain())
        running = asyncio.create_task(self._execute(lease, cancellation))
        try:
            done, _ = await asyncio.wait((heartbeat, running), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            result = await running
            await self.workflows.tick(self.tenant_id, managed_only=self.managed_only)
            return result
        finally:
            stop.set()
            heartbeat.cancel()
            running.cancel()
            await asyncio.gather(heartbeat, running, return_exceptions=True)

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                result = await self.run_once()
            except LeaseLost:
                logger.warning("worker_lease_lost")
                await asyncio.sleep(retry_delay(
                    RetryPolicy(base_delay_seconds=0.1, max_delay_seconds=0.1), 1,
                ))
                continue
            except HarnessError as exc:
                if exc.code not in {
                    "storage_unavailable", "coordination_unavailable", "coordination_timeout", "circuit_open",
                }:
                    raise
                failures += 1
                logger.warning("worker_dependency_unavailable", extra={"failure_category": failure_category(exc.code)})
                await asyncio.sleep(retry_delay(
                    RetryPolicy(base_delay_seconds=1, max_delay_seconds=30), min(failures, 6),
                ))
                continue
            failures = 0
            if result is None:
                if self.signals is None or not await self.signals.wait():
                    await asyncio.sleep(0.5)
