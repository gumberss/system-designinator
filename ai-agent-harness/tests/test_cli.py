import json
import sys

import httpx
import pytest

from agent_harness.cli import main


def test_cli_requires_explicit_credentials_and_prices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in ("OPENAI_API_KEY", "MODEL_INPUT_USD_PER_MILLION", "MODEL_OUTPUT_USD_PER_MILLION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["agent-harness", "task"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "Missing environment variables" in capsys.readouterr().err


@pytest.mark.parametrize("price", ["-1", "NaN", "Infinity", "invalid"])
def test_cli_rejects_bad_price_without_printing_secret(
    price: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-secret")
    monkeypatch.setenv("MODEL_INPUT_USD_PER_MILLION", price)
    monkeypatch.setenv("MODEL_OUTPUT_USD_PER_MILLION", "1")
    monkeypatch.setattr(sys, "argv", ["agent-harness", "task"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "private-test-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("enabled", [True, False])
def test_cli_calculator_is_explicit_opt_in_and_runs_real_tool_locally(
    enabled: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in {
        "OPENAI_API_KEY": "test-only",
        "MODEL_INPUT_USD_PER_MILLION": "1",
        "MODEL_OUTPUT_USD_PER_MILLION": "2",
    }.items():
        monkeypatch.setenv(key, value)
    argv = ["agent-harness", "Add 20 and 22."]
    if enabled:
        argv.append("--enable-calculator")
    monkeypatch.setattr(sys, "argv", argv)
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(req.content)
        assert ("tools" in payload) == enabled
        if enabled and calls == 1:
            return httpx.Response(200, json={
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {"tool_calls": [{
                        "id": "call_sum", "type": "function",
                        "function": {
                            "name": "add_integers_v1",
                            "arguments": '{"left":20,"right":22}',
                        },
                    }]},
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            })
        if enabled:
            tool_message = payload["messages"][-1]
            assert tool_message["role"] == "tool"
            assert json.loads(tool_message["content"])["result"] == {"total": 42}
        return httpx.Response(200, json={
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"completed","content":"42"}'},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    client_type = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        return client_type(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr("agent_harness.cli.httpx.AsyncClient", client_factory)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    assert calls == (2 if enabled else 1)
    output = capsys.readouterr().out
    assert output.startswith("42\n")
    assert "usage_complete=True" in output


def test_cli_anthropic_does_not_require_openai_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-only")
    monkeypatch.setenv("MODEL_INPUT_USD_PER_MILLION", "1")
    monkeypatch.setenv("MODEL_OUTPUT_USD_PER_MILLION", "2")
    monkeypatch.setattr(sys, "argv", ["agent-harness", "--provider", "anthropic", "task"])

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.anthropic.com"
        assert json.loads(req.content)["model"] == "claude-sonnet-4-5-20250929"
        return httpx.Response(200, json={
            "type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"status":"completed","content":"done"}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    client_type = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(handler), trust_env=False)

    monkeypatch.setattr("agent_harness.cli.httpx.AsyncClient", client_factory)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 0
    assert capsys.readouterr().out.startswith("done\n")
