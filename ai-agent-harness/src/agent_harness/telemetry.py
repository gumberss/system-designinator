import asyncio
import hashlib
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import MeterProvider
from opentelemetry.sdk.metrics import MeterProvider as SDKMeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Link, Span, StatusCode, TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agent_harness.domain import Execution, HarnessError, ModelResponse, ModelSelection, Usage, estimate_cost


Operation = Literal[
    "execution", "step", "model", "tool", "tool_invocation", "checkpoint", "context", "worker", "admission", "cancel", "request",
]
execution_id: ContextVar[str | None] = ContextVar("harness_execution_id", default=None)
logger = logging.getLogger(__name__)
_propagator = TraceContextTextMapPropagator()

EVENTS = frozenset({
    "operation_started", "operation_finished", "telemetry_export_failed", "telemetry_shutdown_failed",
    "telemetry_cost_out_of_range",
    "api_unexpected_error",
    "api_request_rejected",
    "redis_notification_unavailable", "redis_signal_unavailable_using_postgres_polling",
    "redis_signal_circuit_open", "redis_signal_circuit_open_using_postgres_polling",
    "worker_lease_lost", "worker_dependency_unavailable", "provider_retry_after_invalid",
    "circuit_probe_abandoned_until_lease_expiry", "circuit_probe_cleanup_unavailable_until_lease_expiry",
})


def failure_category(code: str | None) -> str:
    if code is None:
        return "none"
    if code in {"cancelled", "interrupted"}:
        return "cancelled"
    if "timeout" in code or code in {"deadline_exceeded", "approval_expired"}:
        return "timeout"
    if code in {"access_denied", "tool_access_denied", "provider_authentication", "approval_required",
                "forbidden", "unauthenticated", "egress_denied", "secure_host_required",
                "approval_rejected", "approval_self_forbidden", "approval_action_changed",
                "evaluation_self_forbidden"}:
        return "authorization"
    if code in {"circuit_open", "provider_unavailable", "storage_unavailable", "coordination_unavailable",
                "identity_unavailable", "quota_unavailable", "quota_not_configured"}:
        return "unavailable"
    if code == "provider_rate_limited":
        return "rate_limit"
    if code == "tool_outcome_unknown":
        return "unknown_effect"
    if "limit" in code or "budget" in code or code == "quota_exceeded":
        return "limit"
    if "invalid" in code or code in {
        "provider_refusal", "provider_request_rejected", "output_schema_violation", "approval_conflict",
        "evaluation_artifact_changed", "evaluation_conflict", "evaluation_not_ready",
    }:
        return "validation"
    return "internal"


def provider_label(provider: str) -> str:
    return provider if provider in {"openai", "anthropic"} else "custom"


def identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class SafeJSONFormatter(logging.Formatter):
    """Allowlisted structured fields only, including for SDK/exporter errors."""

    def format(self, record: logging.LogRecord) -> str:
        event = record.msg if isinstance(record.msg, str) and record.msg in EVENTS else "redacted_log"
        if record.name.startswith("opentelemetry"):
            event = "telemetry_export_failed"
        context = trace.get_current_span().get_span_context()
        document: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname, "event": event,
        }
        correlation = execution_id.get()
        if correlation is not None:
            document["execution_id"] = correlation
        request = getattr(record, "request_id", None)
        if isinstance(request, str):
            try:
                document["request_id"] = str(UUID(request))
            except ValueError:
                pass
        if context.is_valid:
            document["trace_id"] = f"{context.trace_id:032x}"
            document["span_id"] = f"{context.span_id:016x}"
        operation = getattr(record, "operation", None)
        if isinstance(operation, str) and operation in {
            "execution", "step", "model", "tool", "tool_invocation", "checkpoint", "context", "worker", "admission", "cancel", "request",
        }:
            document["operation"] = operation
        category = getattr(record, "failure_category", None)
        if isinstance(category, str) and category in {
            "none", "cancelled", "timeout", "authorization", "unavailable", "rate_limit",
            "unknown_effect", "limit", "validation", "internal",
        }:
            document["failure_category"] = category
        return json.dumps(document, allow_nan=False, separators=(",", ":"))


@dataclass
class Observation:
    span: Span
    category: str = "none"

    def fail(self, code: str) -> None:
        self.category = failure_category(code)
        self.span.set_status(StatusCode.ERROR)
        self.span.set_attribute("harness.failure.category", self.category)


class Telemetry:
    def __init__(
        self, tracer_provider: TracerProvider | None = None, meter_provider: MeterProvider | None = None,
    ) -> None:
        self.tracer = (tracer_provider or trace.get_tracer_provider()).get_tracer("agent_harness", "0.2.0")
        meter = (meter_provider or metrics.get_meter_provider()).get_meter("agent_harness", "0.2.0")
        self.operations = meter.create_counter("harness.operations", unit="{operation}")
        self.duration = meter.create_histogram("harness.operation.duration", unit="s")
        self.tokens = meter.create_counter("harness.model.tokens", unit="{token}")
        self.cost = meter.create_counter("harness.model.estimated_cost", unit="USD")
        self.unknown = meter.create_counter("harness.model.unknown_usage", unit="{call}")
        self.queue_age = meter.create_histogram("harness.queue.age", unit="s")
        self.recoveries = meter.create_counter("harness.lease.recoveries", unit="{lease}")
        self.queue_depth = meter.create_gauge("harness.queue.depth", unit="{job}")
        self.circuits = meter.create_counter("harness.circuit.decisions", unit="{decision}")

    @contextmanager
    def operation(
        self, operation: Operation, *, execution: Execution | None = None,
        provider: str | None = None, resource: str | None = None,
        linked_traceparent: str | None = None,
    ) -> Iterator[Observation]:
        token = execution_id.set(str(execution.id)) if execution is not None else None
        attributes: dict[str, str] = {"harness.operation": operation}
        if execution_id.get() is not None:
            attributes["harness.execution.id"] = str(execution_id.get())
        if provider is not None:
            attributes["harness.provider"] = provider_label(provider)
        if resource is not None:
            attributes["harness.resource.hash"] = identifier(resource)
        links: list[Link] = []
        if linked_traceparent:
            linked = trace.get_current_span(_propagator.extract({"traceparent": linked_traceparent})).get_span_context()
            if linked.is_valid:
                links.append(Link(linked))
        started = time.monotonic()
        try:
            with self.tracer.start_as_current_span(
                "harness." + operation, attributes=attributes, links=links,
                record_exception=False, set_status_on_exception=False,
            ) as span:
                observation = Observation(span)
                logger.info("operation_started", extra={"operation": operation})
                try:
                    yield observation
                except BaseException as exc:
                    code = exc.code if isinstance(exc, HarnessError) else (
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else (
                            "deadline_exceeded" if isinstance(exc, TimeoutError) else "internal_error"
                        )
                    )
                    observation.fail(code)
                    raise
                finally:
                    labels = {"operation": operation, "failure_category": observation.category}
                    self.operations.add(1, labels)
                    self.duration.record(time.monotonic() - started, labels)
                    logger.info("operation_finished", extra=labels)
        finally:
            if token is not None:
                execution_id.reset(token)

    def model_usage(self, provider: str, usage: tuple[int, int] | None, cost: float) -> None:
        labels = {"provider": provider_label(provider)}
        if usage is None:
            self.unknown.add(1, labels)
            return
        self.tokens.add(usage[0], {**labels, "direction": "input"})
        self.tokens.add(usage[1], {**labels, "direction": "output"})
        if math.isfinite(cost):
            self.cost.add(cost, labels)
        else:
            logger.warning("telemetry_cost_out_of_range")

    def circuit(self, implementation: Literal["local", "redis"], decision: Literal["granted", "blocked", "probe"]) -> None:
        self.circuits.add(1, {"implementation": implementation, "decision": decision})

    async def model_call(
        self, call: Callable[[], Awaitable[ModelResponse]], execution: Execution, selection: ModelSelection,
        *, attempt_id: str,
    ) -> ModelResponse:
        usage: Usage | None = None
        with self.operation(
            "model", execution=execution, provider=selection.provider, resource=selection.model,
        ) as observation:
            observation.span.set_attribute("harness.model.attempt.id", attempt_id)
            try:
                response = await call()
                usage = response.usage
                return response
            except HarnessError as exc:
                usage = exc.usage
                raise
            finally:
                self.model_usage(
                    selection.provider, (usage.input_tokens, usage.output_tokens) if usage else None,
                    float(estimate_cost(selection, usage)) if usage else 0,
                )


_default = Telemetry()
_current: ContextVar[Telemetry] = ContextVar("harness_telemetry", default=_default)


def telemetry() -> Telemetry:
    return _current.get()


def traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


@contextmanager
def use_telemetry(value: Telemetry) -> Iterator[None]:
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


@contextmanager
def telemetry_environment() -> Iterator[None]:
    """Owned SDK lifecycle, opt-in OTLP; never replace application-global providers."""
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJSONFormatter())
    loggers = [logging.getLogger("agent_harness"), logging.getLogger("opentelemetry")]
    previous = [(item.handlers[:], item.level, item.propagate) for item in loggers]
    for item in loggers:
        item.handlers, item.propagate, item.level = [handler], False, logging.INFO
    tracer_provider: SDKTracerProvider | None = None
    meter_provider: SDKMeterProvider | None = None
    try:
        endpoint = os.environ.get("HARNESS_OTLP_ENDPOINT")
        if endpoint:
            try:
                parsed = urlparse(endpoint)
                port = parsed.port
            except ValueError:
                raise HarnessError("invalid_telemetry_config", "The OTLP endpoint is malformed.") from None
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or port == 0
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})
            ):
                raise HarnessError("invalid_telemetry_config", "Use an HTTPS OTLP endpoint or loopback HTTP without credentials.")
            resource = Resource({
                "service.name": "agent-harness", "service.version": "0.2.0", "service.instance.id": uuid4().hex,
            })
            tracer_provider = SDKTracerProvider(resource=resource, shutdown_on_exit=False)
            tracer_provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=3),
                max_queue_size=2048, max_export_batch_size=256, schedule_delay_millis=1000,
            ))
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=3),
                export_interval_millis=10000, export_timeout_millis=4000,
            )
            meter_provider = SDKMeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
        with use_telemetry(Telemetry(tracer_provider, meter_provider)):
            yield
    finally:
        try:
            if tracer_provider is not None:
                tracer_provider.shutdown()
            if meter_provider is not None:
                meter_provider.shutdown(timeout_millis=5000)
        finally:
            for item, (handlers, level, propagate) in zip(loggers, previous, strict=True):
                item.handlers, item.level, item.propagate = handlers, level, propagate
            handler.close()
