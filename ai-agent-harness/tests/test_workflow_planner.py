from datetime import UTC, datetime, timedelta
from uuid import uuid4

from agent_harness.domain import Failure
from agent_harness.workflows import (
    Condition, NodeRun, WorkflowDefinition, WorkflowNode, WorkflowRun, plan_nodes,
)
from test_runtime import make_agent, principal_for


def workflow() -> WorkflowRun:
    agent = make_agent()
    definition = WorkflowDefinition(nodes=(
        WorkflowNode(id="a", agent=agent, input="first"),
        WorkflowNode(id="b", agent=agent, input="parallel"),
        WorkflowNode(id="join", agent=agent, input="join", depends_on=("a", "b"), handoff_from=("a",)),
        WorkflowNode(id="conditional", agent=agent, input="conditional", depends_on=("a",),
                     condition=Condition(source="a", equals="yes")),
        WorkflowNode(id="descendant", agent=agent, input="descendant", depends_on=("conditional",)),
    ), max_parallelism=2)
    run = WorkflowRun(definition, principal_for(agent))
    run.nodes = {node.id: NodeRun() for node in definition.nodes}
    return run


def test_parallel_admission_waits_for_join_and_applies_condition_without_eval() -> None:
    run = workflow()
    assert [node.id for node in plan_nodes(run, datetime.now(UTC))] == ["a", "b"]
    run.nodes["a"] = NodeRun("queued", uuid4())
    run.nodes["b"] = NodeRun("queued", uuid4())
    assert not plan_nodes(run, datetime.now(UTC))
    run.nodes["a"] = NodeRun("succeeded", uuid4(), "no")
    assert not plan_nodes(run, datetime.now(UTC))
    assert run.nodes["conditional"].status == "skipped"
    assert run.nodes["descendant"].status == "skipped"
    run.nodes["b"] = NodeRun("succeeded", uuid4(), "result")
    assert [node.id for node in plan_nodes(run, datetime.now(UTC))] == ["join"]
    run.nodes["join"] = NodeRun("succeeded", uuid4(), "joined")
    assert not plan_nodes(run, datetime.now(UTC))
    assert run.status == "succeeded"


def test_failure_stops_new_nodes_and_waits_for_inflight_reconciliation() -> None:
    run = workflow()
    run.nodes["a"] = NodeRun("failed", uuid4())
    run.nodes["b"] = NodeRun("queued", uuid4())
    run.failure = Failure("child_failed", "Child failed.")
    assert not plan_nodes(run, datetime.now(UTC))
    assert run.status in {"running"}
    assert run.nodes["join"].status == "cancelled"
    run.nodes["b"].status = "failed"
    assert not plan_nodes(run, datetime.now(UTC))
    assert run.status == "failed"


def test_expired_workflow_never_admits_any_node() -> None:
    run = workflow()
    run.created_at = datetime.now(UTC) - timedelta(days=2)
    assert not plan_nodes(run, datetime.now(UTC))
    assert run.status == "failed"
    assert run.failure is not None and run.failure.code == "workflow_deadline_exceeded"
