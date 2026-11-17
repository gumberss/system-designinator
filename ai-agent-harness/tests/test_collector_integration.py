import os
from urllib.parse import urlparse

import pytest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExporter, MetricExportResult
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

from agent_harness.telemetry import Telemetry


pytestmark = pytest.mark.integration


def test_real_local_collector_accepts_otlp_spans() -> None:
    endpoint = os.environ.get("HARNESS_TEST_OTLP_ENDPOINT")
    if not endpoint:
        pytest.skip("HARNESS_TEST_OTLP_ENDPOINT is required for real Collector integration.")
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Collector integration must target an isolated loopback HTTP endpoint.")
    memory = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "harness-integration"}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    exporter: SpanExporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", timeout=3)
    metric_exporter: MetricExporter = OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=3)
    reader = InMemoryMetricReader()
    meter = MeterProvider(resource=Resource({"service.name": "harness-integration"}), metric_readers=[reader], shutdown_on_exit=False)
    try:
        with Telemetry(provider, meter).operation("execution"):
            pass
        assert exporter.export(memory.get_finished_spans()) == SpanExportResult.SUCCESS
        data = reader.get_metrics_data()
        assert data is not None
        assert metric_exporter.export(data) == MetricExportResult.SUCCESS
    finally:
        exporter.shutdown()
        metric_exporter.shutdown()
        provider.shutdown()
        meter.shutdown()
