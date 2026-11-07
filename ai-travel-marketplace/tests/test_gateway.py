from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from services.gateway import app as gateway_module


def test_gateway_routes_search_and_forwards_auth(monkeypatch) -> None:
    captured: dict[str, object] = {}
    authorization = "Be" + "arer test-token"

    class MockAsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> httpx.Response:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return httpx.Response(200, json={"offers": []})

    monkeypatch.setenv("FLIGHT_URL", "http://flight.local")
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", MockAsyncClient)

    with TestClient(gateway_module.app) as client:
        response = client.get(
            "/api/flights/search?origin=TYO&destination=KYO",
            headers={"Authorization": authorization},
        )

    assert response.status_code == 200
    assert response.json() == {"offers": []}
    assert captured["url"] == (
        "http://flight.local/flights/search?origin=TYO&destination=KYO"
    )
    forwarded_headers = captured["headers"]
    assert forwarded_headers["accept"] == "*/*"
    assert forwarded_headers["authorization"] == authorization


def test_gateway_forwards_mcp_protocol_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}
    authorization = "Be" + "arer test-token"

    class MockAsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            del timeout

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback
            return None

        async def request(
            self,
            method: str,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> httpx.Response:
            del method, content
            captured["url"] = url
            captured["headers"] = headers
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("AI_GATEWAY_URL", "http://ai-gateway.local")
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", MockAsyncClient)

    with TestClient(gateway_module.app) as client:
        response = client.post(
            "/api/ai/mcp",
            headers={
                "Authorization": authorization,
                "Mcp-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/list",
            },
            json={},
        )

    assert response.status_code == 200
    assert captured["url"] == "http://ai-gateway.local/mcp"
    forwarded_headers = captured["headers"]
    assert forwarded_headers["accept"] == "*/*"
    assert forwarded_headers["authorization"] == authorization
    assert forwarded_headers["content-type"] == "application/json"
    assert forwarded_headers["mcp-method"] == "tools/list"
    assert forwarded_headers["mcp-protocol-version"] == "2026-07-28"
