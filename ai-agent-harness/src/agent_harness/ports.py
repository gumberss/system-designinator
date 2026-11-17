from typing import Protocol

from agent_harness.domain import Execution, ModelRequest, ModelResponse


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


class CheckpointWriter(Protocol):
    async def save(self, execution: Execution) -> None: ...
