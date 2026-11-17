from pydantic import Field

from agent_harness.domain import Contract, ToolContext, ToolDefinition
from agent_harness.tools import TypedTool


class AddArguments(Contract):
    left: int = Field(ge=-10**12, le=10**12)
    right: int = Field(ge=-10**12, le=10**12)


class AddResult(Contract):
    total: int


async def _add(arguments: AddArguments, context: ToolContext) -> AddResult:
    return AddResult(total=arguments.left + arguments.right)


def calculator_tool() -> TypedTool[AddArguments, AddResult]:
    return TypedTool(
        ToolDefinition(
            name="add_integers_v1",
            description="Add two bounded integers and return their total.",
            side_effects=False,
        ),
        AddArguments,
        AddResult,
        _add,
    )
