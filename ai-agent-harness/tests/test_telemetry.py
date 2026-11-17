import asyncio
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent_harness.audit import checkpoint_events, execution_metrics
from agent_harness.domain import Execution, HarnessError, ProviderError, RetryPolicy, Status, Usage
from agent_harness.models import ModelExecutor
from agent_harness.runtime import Runtime
from agent_harness.state import ENVELOPE, Envelope
from agent_harness.telemetry import (
    SafeJSONFormatter, Telemetry, execution_id, failure_category, telemetry, telemetry_environment,
    traceparent, use_telemetry,
)
from test_context_persistence import RecordingWriter
from test_dependency_reliability import configure_tool
from test_model_reliability import no_sleep
from test_runtime import BlockingProvider, ScriptedProvider, answer, make_agent, principal_for
from test_tool_runtime import tool_response
from test_tools import build_harness, call


@pytest.fixture
def signals() -> Iterator[tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader]]:
    exporter = InMemorySpanExporter()
    tracer = TracerProvider(resource=Resource({"service.name": "test"}), shutdown_on_exit=False)
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    value = Telemetry(tracer, meter)
    try:
        with use_telemetry(value):
            yield value, exporter, reader
    finally:
        tracer.shutdown()
        meter.shutdown()


def points(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, object], float]]]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        metric.name: [
            (dict(point.attributes or {}), float(getattr(point, "value", getattr(point, "count", 0))))
            for point in metric.data.data_points
        ]
        for resource in data.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics
    }


async def test_runtime_spans_model_metrics_and_correlation_exclude_private_payloads(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, reader = signals
    harness = build_harness()
    private = "secret prompt sk-private user@example.com"
    provider = ScriptedProvider(tool_response(call()), answer(private))
    result = await Runtime(
        {"test-double": provider}, tools=harness.executor,
    ).execute(harness.agent, harness.principal, uuid4(), private)
    spans = exporter.get_finished_spans()
    execution_span = next(span for span in spans if span.name == "harness.execution")
    assert execution_span.context is not None
    assert execution_span.attributes is not None
    assert execution_span.attributes["harness.execution.id"] == str(result.id)
    assert {span.name for span in spans} >= {"harness.execution", "harness.step", "harness.model", "harness.tool"}
    assert all(
        span.context is not None and span.context.trace_id == execution_span.context.trace_id for span in spans
    )
    serialized = "\n".join(span.to_json() for span in spans)
    assert private not in serialized and "user@example.com" not in serialized
    assert harness.principal.subject not in serialized
    assert result.traceparent and str(result.id) in serialized
    assert execution_id.get() is None
    measurements = points(reader)
    tokens = measurements["harness.model.tokens"]
    assert sum(value for attrs, value in tokens if attrs["direction"] == "input") == result.input_tokens
    assert sum(value for attrs, value in tokens if attrs["direction"] == "output") == result.output_tokens
    assert sum(value for _, value in measurements["harness.model.estimated_cost"]) == pytest.approx(
        float(result.estimated_cost_usd),
    )
    for entries in measurements.values():
        assert all(not {"execution_id", "tenant_id", "session_id", "model", "tool", "subject"} & attrs.keys()
                   for attrs, _ in entries)
    assert private not in json.dumps(measurements)


async def test_retries_count_known_error_usage_and_unknown_calls_without_error_body(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, reader = signals
    agent = make_agent().model_copy(update={"retry": RetryPolicy()})
    provider = ScriptedProvider(
        ProviderError("provider_unavailable", "private-secret", usage=Usage(input_tokens=10, output_tokens=2)),
        ProviderError("provider_rate_limited", "private-secret"),
        answer(),
    )
    providers = {agent.selection.provider: provider}
    result = await Runtime(providers, models=ModelExecutor(providers, sleep=no_sleep)).execute(
        agent, principal_for(agent), uuid4(), "private-secret",
    )
    models = [span for span in exporter.get_finished_spans() if span.name == "harness.model"]
    assert len(models) == 3 and [span.status.status_code for span in models] == [
        StatusCode.ERROR, StatusCode.ERROR, StatusCode.UNSET,
    ]
    assert "private-secret" not in "\n".join(span.to_json() for span in models)
    assert all(not span.events and span.status.description is None for span in models)
    measurements = points(reader)
    assert sum(v for a, v in measurements["harness.model.tokens"] if a["direction"] == "input") == 110
    assert sum(v for _, v in measurements["harness.model.unknown_usage"]) == 1
    assert not result.usage_complete


async def test_concurrent_executions_do_not_leak_correlation(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, _ = signals
    agent = make_agent()
    provider = BlockingProvider(expected_calls=2)
    runtime = Runtime({agent.selection.provider: provider})
    tasks = [asyncio.create_task(runtime.execute(agent, principal_for(agent), uuid4(), "task")) for _ in range(2)]
    await asyncio.wait_for(provider.started.wait(), 1)
    provider.release.set()
    results = await asyncio.gather(*tasks)
    models = [span for span in exporter.get_finished_spans() if span.name == "harness.model"]
    assert {str(span.attributes["harness.execution.id"]) for span in models if span.attributes} == {
        str(result.id) for result in results
    }
    assert len({span.context.trace_id for span in models if span.context}) == 2
    assert execution_id.get() is None


async def test_expected_failure_and_cancellation_have_safe_categories(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, _ = signals
    agent = make_agent()
    result = await Runtime({agent.selection.provider: ScriptedProvider(
        ProviderError("provider_refusal", "sensitive refusal details"),
    )}).execute(agent, principal_for(agent), uuid4(), "task")
    assert result.status == Status.FAILED
    execution_span = next(span for span in exporter.get_finished_spans() if span.name == "harness.execution")
    assert execution_span.attributes and execution_span.attributes["harness.failure.category"] == "validation"
    assert execution_span.status.status_code == StatusCode.ERROR
    with pytest.raises(asyncio.CancelledError):
        with telemetry().operation("execution"):
            raise asyncio.CancelledError()
    span = exporter.get_finished_spans()[-1]
    assert span.attributes and span.attributes["harness.failure.category"] == "cancelled"
    assert execution_id.get() is None


async def test_attempt_deadline_is_classified_as_timeout_not_user_cancellation(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, _ = signals
    agent = make_agent(call_timeout_seconds=0.1).model_copy(update={
        "retry": RetryPolicy(max_attempts=1, attempt_timeout_seconds=0.01),
    })
    provider = BlockingProvider()
    result = await Runtime({agent.selection.provider: provider}).execute(
        agent, principal_for(agent), uuid4(), "task",
    )
    assert result.failure and result.failure.code == "provider_timeout"
    span = next(span for span in exporter.get_finished_spans() if span.name == "harness.model")
    assert span.attributes and span.attributes["harness.failure.category"] == "timeout"
    assert provider.stopped.is_set()


def test_json_logging_drops_secrets_in_messages_args_extra_and_tracebacks(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJSONFormatter())
    log = logging.getLogger("test.safe")
    log.addHandler(handler)
    old_level = log.level
    log.setLevel(logging.INFO)
    try:
        with telemetry().operation("execution"):
            try:
                raise ValueError("private-password")
            except ValueError:
                log.exception("Bearer %s", "private-token", extra={"input": "private-input"})
            log.info("operation_finished", extra={"operation": "execution", "failure_category": "none"})
        documents = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert len(documents) == 2 and documents[0]["event"] == "redacted_log"
        assert documents[1]["operation"] == "execution" and documents[1]["trace_id"]
        assert "private-" not in stream.getvalue() and "Traceback" not in stream.getvalue()
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)


def test_json_logging_does_not_crash_on_hostile_non_scalar_extras() -> None:
    record = logging.LogRecord("agent_harness", logging.ERROR, "", 1, {"secret": "private"}, (), None)
    record.operation = ["private"]
    record.failure_category = {"private": "password"}
    document = json.loads(SafeJSONFormatter().format(record))
    assert document["event"] == "redacted_log" and "private" not in json.dumps(document)


def test_job_traceparent_can_be_linked_without_becoming_new_parent(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, _ = signals
    with telemetry().operation("admission"):
        carrier = traceparent()
        assert carrier
    with telemetry().operation("worker", linked_traceparent=carrier):
        pass
    first, second = exporter.get_finished_spans()
    assert first.context and second.context and first.context.trace_id != second.context.trace_id
    assert second.parent is None and len(second.links) == 1
    assert second.links[0].context.trace_id == first.context.trace_id


async def test_checkpoint_roundtrip_keeps_trace_links_and_model_attempt_audit_has_no_payload(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    agent = make_agent()
    writer = RecordingWriter()
    result = await Runtime({agent.selection.provider: ScriptedProvider(answer("private-result"))}).execute(
        agent, principal_for(agent), uuid4(), "private-input", checkpoint=writer,
    )
    restored = ENVELOPE.validate_json(ENVELOPE.dump_json(Envelope(
        agent, principal_for(agent), "private-input", result,
    )))
    assert restored.execution.traceparent == result.traceparent
    events = checkpoint_events(result)
    assert any(event.kind == "model.succeeded" for event in events)
    assert "private-" not in repr(events)
    measurements = execution_metrics(result)
    assert measurements["estimated_cost_usd"] == str(result.estimated_cost_usd)
    assert measurements["usage_complete"] is True
    assert "private-" not in repr(measurements)


async def test_checkpoint_failure_marks_parent_trace_as_failed_and_propagates(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    _, exporter, _ = signals
    agent = make_agent()

    class RejectingWriter:
        async def save(self, execution: Execution) -> None:
            raise HarnessError("storage_unavailable", "private-connection-string")

    with pytest.raises(HarnessError):
        await Runtime({agent.selection.provider: ScriptedProvider(answer())}).execute(
            agent, principal_for(agent), uuid4(), "private-prompt", checkpoint=RejectingWriter(),
        )
    span = next(span for span in exporter.get_finished_spans() if span.name == "harness.execution")
    assert span.status.status_code == StatusCode.ERROR
    assert "private" not in span.to_json()


async def test_tool_retry_has_durable_attempt_outcomes_and_distinct_audit_keys(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    harness = build_harness()
    harness.handler.dependency_timeout = True
    harness.tool = replace(
        harness.tool, definition=harness.tool.definition.model_copy(update={
            "retry": RetryPolicy(max_attempts=2, base_delay_seconds=0),
        }),
    )
    configure_tool(harness)
    with pytest.raises(HarnessError):
        await harness.invoke(call())
    records = harness.execution.tool_invocations[0].attempt_records
    assert len(records) == 2 and all(record.status == "failed" for record in records)
    assert all(record.error_code == "tool_timeout" and record.finished_at for record in records)
    events = checkpoint_events(harness.execution)
    assert len({event.key for event in events}) == len(events)
    assert sum(event.kind == "tool_attempt.failed" for event in events) == 2


@pytest.mark.parametrize("endpoint", [
    "http://untrusted.invalid", "https://user:password@localhost", "https://localhost?token=private",
    "file:///private", "https://localhost#private",
    "https://[private", "https://localhost:private", "http://localhost:0",
])
def test_telemetry_endpoint_rejects_plaintext_remote_and_inline_credentials(
    endpoint: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_OTLP_ENDPOINT", endpoint)
    with pytest.raises(HarnessError) as error:
        with telemetry_environment():
            pytest.fail("Unsafe telemetry configuration must be rejected.")
    assert error.value.code == "invalid_telemetry_config" and "private" not in str(error.value)


def test_logging_setup_restores_embedding_host_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_OTLP_ENDPOINT", raising=False)
    log = logging.getLogger("agent_harness")
    original = (log.handlers[:], log.level, log.propagate)
    global_provider = trace.get_tracer_provider()
    with telemetry_environment():
        assert len(log.handlers) == 1 and isinstance(log.handlers[0].formatter, SafeJSONFormatter)
        assert not log.propagate
    assert (log.handlers, log.level, log.propagate) == original
    assert trace.get_tracer_provider() is global_provider


@pytest.mark.parametrize(("code", "category"), [
    ("provider_timeout", "timeout"), ("tool_access_denied", "authorization"),
    ("tool_outcome_unknown", "unknown_effect"), ("private-secret-code", "internal"),
    ("approval_expired", "timeout"), ("approval_rejected", "authorization"),
    ("approval_self_forbidden", "authorization"), ("approval_action_changed", "authorization"),
    ("approval_conflict", "validation"),
    ("evaluation_self_forbidden", "authorization"), ("evaluation_artifact_changed", "validation"),
    ("evaluation_conflict", "validation"), ("evaluation_not_ready", "validation"),
])
def test_failure_classification_is_bounded(code: str, category: str) -> None:
    assert failure_category(code) == category


def test_unbounded_provider_and_resource_names_do_not_become_metric_labels(
    signals: tuple[Telemetry, InMemorySpanExporter, InMemoryMetricReader],
) -> None:
    value, exporter, reader = signals
    for index in range(64):
        private_name = f"private-provider-{index}"
        value.model_usage(private_name, (1, 2), 0.1)
        with value.operation("model", provider=private_name, resource=private_name):
            pass
    measurements = points(reader)
    assert len(measurements["harness.model.tokens"]) == 2
    assert len(measurements["harness.model.estimated_cost"]) == 1
    assert len(measurements["harness.operations"]) == 1
    assert "private-provider" not in json.dumps(measurements)
    assert "private-provider" not in "\n".join(span.to_json() for span in exporter.get_finished_spans())
