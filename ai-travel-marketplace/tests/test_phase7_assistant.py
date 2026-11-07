from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi.testclient import TestClient

from packages.common.booking import utc_now
from packages.common.security import create_access_token, current_user_id
from services.ai_gateway import app as ai_gateway_module
from services.ai_gateway import assistant_mcp
from services.ai_gateway.assistant_state import AssistantQuote
from services.ai_gateway.provider import OpenAIProvider, ToolCall, ToolCompletion


def test_openai_tool_calls_are_parsed_and_sent_with_schemas() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "tool-call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_travel",
                                        "arguments": '{"query":"Tokyo"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://openai.example/v1",
            transport=httpx.MockTransport(handler),
        ) as client:
            provider = OpenAIProvider(
                client,
                chat_model="chat-test",
                embedding_model="embedding-test",
                embedding_dimensions=2,
            )
            return await provider.complete_with_tools(
                [{"role": "user", "content": "Search Tokyo"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "search_travel",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            )

    completion = asyncio.run(run())

    assert captured["tool_choice"] == "auto"
    assert captured["tools"][0]["function"]["name"] == "search_travel"
    assert completion.content == ""
    assert completion.tool_calls == [
        ToolCall("tool-call-1", "search_travel", {"query": "Tokyo"})
    ]


def _configure_assistant(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'assistant.db'}")
    monkeypatch.setenv("JWT_SECRET", "test-phase7-secret-long-enough-for-signing")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    async def skip_knowledge_setup(app: Any) -> None:
        app.state.runtime["knowledge_ready"] = True
        app.state.runtime["knowledge_chunks"] = 1
        app.state.runtime["degraded_reason"] = None

    monkeypatch.setattr(ai_gateway_module, "_prepare_knowledge", skip_knowledge_setup)


def test_assistant_selects_mcp_search_tool_and_scopes_conversation(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    _configure_assistant(monkeypatch, tmp_path)
    retrieved_questions: list[str] = []

    async def fake_retrieve(
        question: str,
        provider: Any,
        qdrant: Any,
    ) -> list[dict[str, Any]]:
        assert provider is not None
        assert qdrant is not None
        retrieved_questions.append(question)
        return []

    async def fake_platform_request(
        service: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert service == "SEARCH_URL"
        assert method == "GET"
        assert path == "/search"
        assert kwargs["params"]["query"] == "Tokyo"
        return {"results": [{"id": "destination-tokyo"}]}

    class FakeProvider:
        chat_model = "chat-test"

        def __init__(self) -> None:
            self.turn = 0

        async def complete_with_tools(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> ToolCompletion:
            names = {tool["function"]["name"] for tool in tools}
            assert "search_travel" in names
            assert "start_booking" not in names
            self.turn += 1
            if self.turn == 1:
                return ToolCompletion(
                    content="",
                    tool_calls=[
                        ToolCall(
                            call_id="search-call-1",
                            name="search_travel",
                            arguments={"query": "Tokyo", "kind": "destination"},
                        )
                    ],
                )
            assert any(message.get("role") == "tool" for message in messages)
            return ToolCompletion(
                content="I found destination results for Tokyo.",
                tool_calls=[],
            )

    monkeypatch.setattr(ai_gateway_module, "retrieve_relevant_chunks", fake_retrieve)
    monkeypatch.setattr(assistant_mcp, "_platform_request", fake_platform_request)
    authorization = "Be" + "arer " + create_access_token("traveler-1")
    assert current_user_id(authorization) == "traveler-1"
    headers = {"Authorization": authorization}

    with TestClient(ai_gateway_module.app) as client:
        client.app.state.provider = FakeProvider()
        unauthorized_mcp = client.post("/mcp", json={})
        mcp_listing = client.post(
            "/mcp/",
            headers={
                **headers,
                "Host": "ai_gateway:8009",
                "Accept": "application/json, text/event-stream",
                "Mcp-Method": "tools/list",
                "Mcp-Protocol-Version": "2026-07-28",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert mcp_listing.request.headers.get("authorization") == authorization
        response = client.post(
            "/assistant/chat",
            headers=headers,
            json={"message": "Find places to visit in Tokyo"},
        )

        assert unauthorized_mcp.status_code == 401
        assert mcp_listing.status_code == 200, mcp_listing.text
        assert "search_travel" in str(mcp_listing.json())
        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]
        assert response.json()["answer"] == "I found destination results for Tokyo."
        stored = client.get(
            f"/assistant/conversations/{conversation_id}",
            headers=headers,
        )
        other_user = client.get(
            f"/assistant/conversations/{conversation_id}",
            headers={
                "Authorization": (
                    "Be" + "arer " + create_access_token("traveler-2")
                ),
            },
        )

    assert stored.status_code == 200
    assert retrieved_questions == ["Find places to visit in Tokyo"]
    assert [item["role"] for item in stored.json()["messages"]] == [
        "user",
        "assistant",
    ]
    assert other_user.status_code == 404


def test_quote_requires_exact_authenticated_confirmation_and_is_idempotent(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    _configure_assistant(monkeypatch, tmp_path)
    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_platform_request(
        service: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(
            (service, method, path, kwargs.get("idempotency_key"))
        )
        return {
            "id": "booking-1",
            "quote_id": "quote-1",
            "status": "confirmed",
            "total_amount": "125.00",
            "currency": "USD",
        }

    monkeypatch.setattr(assistant_mcp, "_platform_request", fake_platform_request)
    headers = {
        "Authorization": f"Bearer {create_access_token('traveler-1')}",
    }

    with TestClient(ai_gateway_module.app) as client:
        quote = AssistantQuote(
            id="quote-1",
            user_id="traveler-1",
            fingerprint="a" * 64,
            total_amount=Decimal("125.00"),
            currency="USD",
            expires_at=utc_now() + timedelta(minutes=10),
            status="pending",
            idempotency_key="assistant-test-quote-1",
            created_at=utc_now(),
        )
        with client.app.state.session_factory() as session:
            session.add(quote)
            session.commit()

        wrong_phrase = client.post(
            "/assistant/confirm",
            headers=headers,
            json={
                "quote_id": "quote-1",
                "confirmation_phrase": "CONFIRM quote-1 124.00 USD",
            },
        )
        wrong_owner = client.post(
            "/assistant/confirm",
            headers={
                "Authorization": f"Bearer {create_access_token('traveler-2')}",
            },
            json={
                "quote_id": "quote-1",
                "confirmation_phrase": "CONFIRM quote-1 125.00 USD",
            },
        )
        payload = {
            "quote_id": "quote-1",
            "confirmation_phrase": "CONFIRM quote-1 125.00 USD",
        }
        confirmed = client.post("/assistant/confirm", headers=headers, json=payload)
        replay = client.post("/assistant/confirm", headers=headers, json=payload)

    assert wrong_phrase.status_code == 400
    assert wrong_owner.status_code == 404
    assert confirmed.status_code == 200
    assert confirmed.json()["id"] == "booking-1"
    assert replay.status_code == 200
    assert replay.json() == confirmed.json()
    assert calls == [
        ("BOOKING_URL", "POST", "/bookings", "assistant-test-quote-1")
    ]
