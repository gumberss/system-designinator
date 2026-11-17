from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, model_validator

from agent_harness.domain import Agent, Contract, Failure, Principal


class Condition(Contract):
    source: str
    equals: str


class WorkflowNode(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    agent: Agent
    input: str = Field(min_length=1, max_length=65536)
    depends_on: tuple[str, ...] = ()
    handoff_from: tuple[str, ...] = ()
    condition: Condition | None = None


class WorkflowDefinition(Contract):
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1, max_length=32)
    max_parallelism: int = Field(default=4, ge=1, le=16)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("Workflow node IDs must be unique.")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in visiting:
                raise ValueError("Workflow cycles are forbidden.")
            if identifier in visited:
                return
            node = by_id[identifier]
            if (
                len(set(node.depends_on)) != len(node.depends_on)
                or not set(node.depends_on) <= by_id.keys()
                or not set(node.handoff_from) <= set(node.depends_on)
                or (node.condition is not None and node.condition.source not in node.depends_on)
            ):
                raise ValueError("Dependencies, handoffs or condition source are invalid.")
            visiting.add(identifier)
            for dependency in node.depends_on:
                visit(dependency)
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in by_id:
            visit(identifier)
        return self


@dataclass
class NodeRun:
    status: Literal["pending", "queued", "succeeded", "failed", "cancelled", "skipped"] = "pending"
    execution_id: UUID | None = None
    result: str | None = None


@dataclass
class WorkflowRun:
    definition: WorkflowDefinition
    principal: Principal
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: Literal["running", "succeeded", "failed"] = "running"
    nodes: dict[str, NodeRun] = field(default_factory=dict)
    failure: Failure | None = None
    schema_version: Literal[1] = 1
    traceparent: str | None = None
    managed: bool = False


WORKFLOW = TypeAdapter(WorkflowRun)


def plan_nodes(run: WorkflowRun, now: datetime) -> tuple[WorkflowNode, ...]:
    if now >= run.created_at + timedelta(seconds=run.definition.timeout_seconds):
        run.failure = Failure("workflow_deadline_exceeded", "Workflow deadline exceeded.")
    if run.failure is not None:
        for node in run.nodes.values():
            if node.status == "pending":
                node.status = "cancelled"
        if not any(node.status == "queued" for node in run.nodes.values()):
            run.status = "failed"
        return ()
    active = sum(node.status == "queued" for node in run.nodes.values())
    ready = []
    for spec in run.definition.nodes:
        node = run.nodes[spec.id]
        if node.status != "pending":
            continue
        dependencies = [run.nodes[name] for name in spec.depends_on]
        if any(item.status == "skipped" for item in dependencies):
            node.status = "skipped"
            continue
        if not all(item.status == "succeeded" for item in dependencies):
            continue
        if spec.condition and run.nodes[spec.condition.source].result != spec.condition.equals:
            node.status = "skipped"
            continue
        if active < run.definition.max_parallelism:
            ready.append(spec)
            active += 1
    if all(node.status in ("succeeded", "skipped") for node in run.nodes.values()):
        run.status = "succeeded"
    return tuple(ready)
