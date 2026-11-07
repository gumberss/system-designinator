from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from packages.common.booking import utc_now
from packages.common.database import build_engine
from packages.common.security import create_access_token
from services.ai_gateway.assistant_state import (
    AssistantBase,
    Conversation,
    purge_expired_conversations,
)
from services.gateway import app as gateway_module


def test_conversation_retention_removes_idle_history_at_90_day_boundary(
    tmp_path,
) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'retention.db'}")
    AssistantBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = utc_now()
    with factory.begin() as session:
        session.add_all(
            [
                Conversation(
                    id="expired-conversation",
                    user_id="traveler-1",
                    messages=[],
                    created_at=now,
                    updated_at=now - timedelta(days=90),
                ),
                Conversation(
                    id="active-conversation",
                    user_id="traveler-1",
                    messages=[],
                    created_at=now,
                    updated_at=now - timedelta(days=89),
                ),
            ]
        )

    assert purge_expired_conversations(factory, now=now) == 1
    with factory() as session:
        remaining_ids = set(session.scalars(select(Conversation.id)))
    engine.dispose()

    assert remaining_ids == {"active-conversation"}


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_detail"),
    [
        ("timeout", 504, "Marketplace service timed out"),
        ("connection", 503, "Marketplace service is unavailable"),
    ],
)
def test_gateway_surfaces_injected_upstream_failures(
    monkeypatch,
    failure: str,
    expected_status: int,
    expected_detail: str,
) -> None:
    class FailingAsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            del timeout

        async def __aenter__(self) -> FailingAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

        async def request(self, method: str, url: str, **kwargs):
            del method, kwargs
            request = httpx.Request("GET", url)
            if failure == "timeout":
                raise httpx.TimeoutException("injected timeout", request=request)
            raise httpx.ConnectError("injected connection failure", request=request)

    monkeypatch.setenv("ACTIVITIES_URL", "http://activities.local")
    monkeypatch.setenv(
        "JWT_SECRET", "test-phase8-secret-long-enough-for-signing"
    )
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", FailingAsyncClient)
    token = create_access_token("traveler-1")

    with TestClient(gateway_module.app) as client:
        response = client.get(
            "/api/activities/search?city_code=TYO",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
