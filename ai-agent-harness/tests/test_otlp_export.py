import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from google.protobuf.json_format import MessageToDict

from agent_harness.runtime import Runtime
from agent_harness.telemetry import telemetry_environment
from test_runtime import ScriptedProvider, answer, make_agent, principal_for
from uuid import uuid4


@pytest.fixture
def receiver() -> Iterator[tuple[str, Queue[tuple[str, bytes]], list[int]]]:
    requests: Queue[tuple[str, bytes]] = Queue()
    status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            requests.put((self.path, body))
            response_body = b"" if status[0] == 200 else b"private-exporter-error-body"
            self.send_response(status[0])
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, status
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


async def test_real_otlp_http_exporters_send_valid_protobuf_without_private_data(
    receiver: tuple[str, Queue[tuple[str, bytes]], list[int]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint, requests, _ = receiver
    monkeypatch.setenv("HARNESS_OTLP_ENDPOINT", endpoint)
    agent = make_agent()
    with telemetry_environment():
        result = await Runtime({agent.selection.provider: ScriptedProvider(
            answer("private-model-result"),
        )}).execute(agent, principal_for(agent), uuid4(), "private-user-prompt")
    documents = {}
    while not requests.empty():
        path, body = requests.get_nowait()
        if path == "/v1/traces":
            documents[path] = MessageToDict(ExportTraceServiceRequest.FromString(body))
        elif path == "/v1/metrics":
            documents[path] = MessageToDict(ExportMetricsServiceRequest.FromString(body))
    assert set(documents) == {"/v1/traces", "/v1/metrics"}
    serialized = json.dumps(documents)
    assert "private-user-prompt" not in serialized and "private-model-result" not in serialized
    assert "harness.execution" in serialized and str(result.id) in serialized
    assert "harness.model.tokens" in serialized and "harness.model.estimated_cost" in serialized


async def test_exporter_failure_does_not_change_success_and_logs_no_error_body(
    receiver: tuple[str, Queue[tuple[str, bytes]], list[int]], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    endpoint, requests, status = receiver
    status[0] = 400
    monkeypatch.setenv("HARNESS_OTLP_ENDPOINT", endpoint)
    agent = make_agent()
    with telemetry_environment():
        result = await Runtime({agent.selection.provider: ScriptedProvider(answer())}).execute(
            agent, principal_for(agent), uuid4(), "private-user-prompt",
        )
    assert result.result == "done" and requests.qsize() >= 2
    logs = capsys.readouterr().err
    assert "telemetry_export_failed" in logs
    assert "private-user-prompt" not in logs and endpoint not in logs
    assert "private-exporter-error-body" not in logs
