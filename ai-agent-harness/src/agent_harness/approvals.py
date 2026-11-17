import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

from agent_harness.domain import (
    Agent, ApprovalPolicy, ApprovalRequest, Execution, HarnessError,
    ToolCall, ToolDefinition, ToolDescriptor, ToolInvocation, execution_deadline,
)
from agent_harness.json_codec import decode_object


class ApprovalAuthorizer(Protocol):
    async def consume(
        self, execution: Execution, invocation: ToolInvocation, descriptor: ToolDescriptor,
    ) -> None: ...


def requires_approval(agent: Agent, definition: ToolDefinition) -> bool:
    policy = agent.approval_policy
    return definition.requires_approval or policy is not None and (
        definition.name in policy.required_tools or policy.require_side_effects and definition.side_effects
    )


def definition_digest(descriptor: ToolDescriptor) -> str:
    return hashlib.sha256(json.dumps({
        "definition": descriptor.definition.model_dump(mode="json"),
        "input": descriptor.input_schema, "output": descriptor.output_schema,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def request_for(
    execution: Execution, agent: Agent, invocation: ToolInvocation,
    call: ToolCall, descriptor: ToolDescriptor,
) -> ApprovalRequest:
    arguments = json.dumps(decode_object(call.arguments), sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = definition_digest(descriptor)
    request = next((item for item in execution.approvals if item.invocation_id == invocation.id), None)
    if request is None:
        policy = agent.approval_policy or ApprovalPolicy()
        request = ApprovalRequest(
            execution.id, agent.id, agent.version, invocation.id, call.id, invocation.name,
            descriptor.definition.version, arguments,
            hashlib.sha256(arguments.encode()).hexdigest(), digest, execution.subject,
            min(execution_deadline(execution, agent), datetime.now(UTC) + timedelta(seconds=policy.timeout_seconds)),
        )
        execution.approvals.append(request)
    if (
        request.arguments_sha256 != invocation.arguments_sha256 or request.arguments != arguments
        or request.definition_sha256 != digest or request.tool_name != invocation.name
        or request.tool_version != descriptor.definition.version or request.requested_by != execution.subject
        or request.call_id != call.id
        or request.execution_id != execution.id or request.agent_id != agent.id or request.agent_version != agent.version
    ):
        raise HarnessError("approval_action_changed", "Approval does not match the exact proposed action.")
    if request.status in ("cancelled", "expired") or datetime.now(UTC) >= request.expires_at:
        raise HarnessError("approval_expired", "Approval is no longer valid.")
    if request.status == "rejected":
        raise HarnessError("approval_rejected", "The operation was rejected by a human.")
    return request
