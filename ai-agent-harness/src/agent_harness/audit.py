from dataclasses import dataclass
from datetime import datetime

from agent_harness.domain import Execution
from agent_harness.telemetry import failure_category, identifier, provider_label


@dataclass(frozen=True)
class AuditEvent:
    key: str
    kind: str
    data: dict[str, object]


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    actor: str
    created_at: datetime
    data: dict[str, object]


def checkpoint_events(execution: Execution) -> tuple[AuditEvent, ...]:
    events: list[AuditEvent] = []
    for attempt in execution.model_call_records:
        events.append(AuditEvent(f"model:{attempt.id}:{attempt.status}", "model." + attempt.status, {
            "attempt_id": str(attempt.id), "provider": provider_label(attempt.selection.provider),
            "model_hash": identifier(attempt.selection.model), "purpose": attempt.purpose,
            "step": attempt.step, "input_tokens": attempt.usage.input_tokens if attempt.usage else None,
            "output_tokens": attempt.usage.output_tokens if attempt.usage else None,
            "estimated_cost_usd": str(attempt.estimated_cost_usd),
            "failure_category": failure_category(attempt.error_code),
            "started_at": attempt.started_at.isoformat(),
            "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
        }))
    for invocation in execution.tool_invocations:
        for dispatch in invocation.attempt_records:
            events.append(AuditEvent(
                f"dispatch:{invocation.id}:{dispatch.index}:{dispatch.status}", "tool_attempt." + dispatch.status,
                {
                    "invocation_id": str(invocation.id), "attempt": dispatch.index,
                    "failure_category": failure_category(dispatch.error_code),
                    "latency_seconds": (
                        (dispatch.finished_at - dispatch.started_at).total_seconds() if dispatch.finished_at else None
                    ),
                    "started_at": dispatch.started_at.isoformat(),
                    "finished_at": dispatch.finished_at.isoformat() if dispatch.finished_at else None,
                },
            ))
        events.append(AuditEvent(
            f"tool:{invocation.id}:{invocation.attempts}:{invocation.status}:{invocation.replay_count}",
            "tool.replayed" if invocation.replay_count else "tool." + invocation.status, {
                "invocation_id": str(invocation.id), "tool_hash": identifier(invocation.name),
                "attempt": invocation.attempts, "replay_count": invocation.replay_count,
                "side_effects": invocation.side_effects,
                "failure_category": failure_category(invocation.failure.code if invocation.failure else None),
            },
        ))
    return tuple(events)


def execution_metrics(execution: Execution) -> dict[str, object]:
    return {
        "execution_id": str(execution.id), "status": execution.status.value,
        "model_steps": execution.model_attempts, "model_calls": execution.model_calls,
        "tool_calls": execution.tool_call_attempts, "tool_dispatches": execution.tool_dispatches,
        "input_tokens": execution.input_tokens, "output_tokens": execution.output_tokens,
        "estimated_cost_usd": str(execution.estimated_cost_usd),
        "usage_complete": execution.usage_complete, "unaccounted_calls": execution.unaccounted_calls,
        "failure_category": failure_category(execution.failure.code if execution.failure else None),
        "traceparent": execution.traceparent,
        "duration_seconds": (
            (execution.finished_at - execution.created_at).total_seconds() if execution.finished_at else None
        ),
    }
