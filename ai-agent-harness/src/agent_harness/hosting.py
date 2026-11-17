import hashlib
import os
from collections.abc import Callable, Iterable

import httpx
from pydantic import SecretStr

from agent_harness.adapters.anthropic import AnthropicProvider
from agent_harness.adapters.openai import OpenAIProvider
from agent_harness.domain import HarnessError, ModelSelection
from agent_harness.ports import ModelProvider


def configure_providers(
    client: httpx.AsyncClient, selections: Iterable[ModelSelection],
) -> tuple[dict[str, ModelProvider], dict[str, str]]:
    factories: dict[str, Callable[[httpx.AsyncClient, SecretStr], ModelProvider]] = {
        "openai": OpenAIProvider, "anthropic": AnthropicProvider,
    }
    providers: dict[str, ModelProvider] = {}
    scopes: dict[str, str] = {}
    for selection in selections:
        if selection.provider not in factories:
            raise HarnessError("provider_not_configured", "The host does not install this provider.")
        if selection.capabilities is not None and selection.capabilities.residency != "global":
            raise HarnessError(
                "unsupported_residency", "This host only installs global public provider endpoints.",
            )
        if selection.provider in providers:
            continue
        key = os.environ.get(selection.provider.upper() + "_API_KEY", "")
        if not key.strip():
            raise HarnessError("missing_provider_credentials", "Configure credentials for every configured provider.")
        providers[selection.provider] = factories[selection.provider](client, SecretStr(key))
        scopes[selection.provider] = selection.provider + ":" + hashlib.sha256(key.encode()).hexdigest()
    return providers, scopes
