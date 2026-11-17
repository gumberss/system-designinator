import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Generic, Literal, TypeAlias, TypeVar
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field, ValidationError, field_validator
from redis.asyncio import Redis
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from agent_harness.adapters.calculator import calculator_tool
from agent_harness.adapters.postgres import PostgresStore
from agent_harness.adapters.rate_limit import RateLimiter
from agent_harness.adapters.security_postgres import SecurityRepository
from agent_harness.adapters.workflow_postgres import WorkflowStore
from agent_harness.adapters.approvals_postgres import ApprovalRepository
from agent_harness.adapters.evaluations_postgres import EvaluationRepository
from agent_harness.evaluation import ExecutionArtifact, HumanEvaluation, HumanEvaluationCreate, EvaluationRubric, RUBRIC
from agent_harness.audit import execution_metrics
from agent_harness.domain import Agent, ApprovalPolicy, ApprovalRequest, Contract, Execution, HarnessError, Limits, Status, ToolDefinition
from agent_harness.state import HistoryMessage, Summary
from agent_harness.json_codec import decode_object
from agent_harness.security import (
    Access, JWKSClient, ServerConfig, narrowed_limits, validate_result_schema,
)
from agent_harness.telemetry import failure_category, telemetry, telemetry_environment
from agent_harness.tools import ToolRegistry
from agent_harness.workflows import Condition, NodeRun, WorkflowDefinition, WorkflowNode


logger = logging.getLogger(__name__)
T = TypeVar("T")
ZERO = UUID(int=0)


class Page(Contract, Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None = None


class Problem(Contract):
    type: str
    title: str
    status: int = Field(ge=400, le=599)
    code: str
    detail: str
    correlation_id: UUID


class AgentSpec(Contract):
    instructions: str = Field(min_length=1, max_length=16384)
    model_key: str = Field(min_length=1, max_length=100)
    fallback_keys: tuple[str, ...] = Field(default=(), max_length=3)
    allowed_tools: frozenset[str] = frozenset()
    limits: Limits = Field(default_factory=Limits)
    output_schema: dict[str, object] | None = None
    approval_policy: ApprovalPolicy | None = None

    @field_validator("output_schema")
    @classmethod
    def checked_schema(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is not None:
            validate_result_schema(value)
        return value


class AgentCreate(AgentSpec):
    id: UUID


class ToolRegistration(Contract):
    implementation_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    enabled: bool = True


class ToolUpdate(Contract):
    enabled: bool


class ToolRead(Contract):
    implementation_key: str
    revision: int
    enabled: bool
    definition: ToolDefinition
    input_schema: dict[str, object]
    output_schema: dict[str, object]


class SessionRead(Contract):
    id: UUID


class ContextRead(Contract):
    history: tuple[HistoryMessage, ...]
    summary: Summary | None
    memories: tuple[str, ...]


class QuotaRead(Contract):
    executions: int
    tokens: int
    cost: Decimal
    active: int


class WorkflowRead(Contract):
    id: UUID
    status: Literal["running", "succeeded", "failed"]
    created_at: datetime
    nodes: dict[str, NodeRun]
    failure_category: str | None


class MemoryWrite(Contract):
    content: str = Field(min_length=1, max_length=8192)
    ttl_seconds: int = Field(default=86400, ge=1, le=2592000)


class ExecutionCreate(Contract):
    agent_id: UUID
    agent_version: int | None = Field(default=None, ge=1)
    session_id: UUID
    input: str = Field(min_length=1, max_length=65536)
    limits: Limits | None = None
    output_schema: dict[str, object] | None = None

    @field_validator("output_schema")
    @classmethod
    def checked_schema(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        return AgentSpec.checked_schema(value)


class Accepted(Contract):
    execution_id: UUID
    status_url: str


class ExecutionRead(Contract):
    id: UUID
    agent_id: UUID
    agent_version: int
    session_id: UUID
    status: Status
    created_at: datetime
    finished_at: datetime | None
    result: str | None
    failure_category: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: str
    usage_complete: bool
    pending_approval_ids: tuple[UUID, ...] = ()

    @classmethod
    def from_execution(cls, execution: Execution) -> "ExecutionRead":
        from agent_harness.telemetry import failure_category
        return cls(
            id=execution.id, agent_id=execution.agent_id, agent_version=execution.agent_version,
            session_id=execution.session_id, status=execution.status, created_at=execution.created_at,
            finished_at=execution.finished_at, result=execution.result,
            failure_category=failure_category(execution.failure.code) if execution.failure else None,
            input_tokens=execution.input_tokens, output_tokens=execution.output_tokens,
            estimated_cost_usd=str(execution.estimated_cost_usd), usage_complete=execution.usage_complete,
            pending_approval_ids=tuple(item.id for item in execution.approvals if item.status == "pending"),
        )


class MetricsRead(Contract):
    execution_id: str
    status: str
    model_steps: int
    model_calls: int
    tool_calls: int
    tool_dispatches: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: str
    usage_complete: bool
    unaccounted_calls: int
    failure_category: str
    duration_seconds: float | None
    traceparent: str | None


class TraceRead(Contract):
    sequence: int
    kind: str
    actor: str
    created_at: datetime
    data: dict[str, object]


class WorkflowNodeCreate(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    agent_id: UUID
    input: str = Field(min_length=1, max_length=65536)
    depends_on: tuple[str, ...] = ()
    handoff_from: tuple[str, ...] = ()
    condition: Condition | None = None


class WorkflowCreate(Contract):
    nodes: tuple[WorkflowNodeCreate, ...] = Field(min_length=1, max_length=32)
    max_parallelism: int = Field(default=4, ge=1, le=16)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)


class ApprovalDecision(Contract):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonblank decision reason is required.")
        return value


def etag(value: str | None) -> int:
    if value is None:
        raise HarnessError("precondition_required", "If-Match is required.")
    if len(value) > 16 or not value.startswith('"') or not value.endswith('"') or not value[1:-1].isdigit():
        raise HarnessError("invalid_precondition", "If-Match must contain a quoted positive version.")
    version = int(value[1:-1])
    if version < 1:
        raise HarnessError("invalid_precondition", "If-Match must contain a quoted positive version.")
    return version


def problem(status: int, code: str, correlation_id: str) -> JSONResponse:
    response = JSONResponse(Problem(
        type="urn:agent-harness:" + code, title="Request failed",
        status=status, code=code, detail="The request could not be completed.",
        correlation_id=UUID(correlation_id),
    ).model_dump(mode="json"), status_code=status, media_type="application/problem+json")
    response.headers.update({
        "X-Correlation-ID": correlation_id, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })
    if status == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    if status == 429:
        response.headers["Retry-After"] = "60"
    return response


class RequestBoundary:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation = str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                while True:
                    chunk = await receive()
                    if chunk["type"] == "http.disconnect":
                        return
                    body.extend(chunk.get("body", b""))
                    if len(body) > 262144:
                        await problem(413, "request_too_large", correlation)(scope, receive, send)
                        return
                    if not chunk.get("more_body", False):
                        break
        except TimeoutError:
            await problem(408, "request_timeout", correlation)(scope, receive, send)
            return
        if body:
            try:
                decode_object(body.decode("utf-8"))
            except (ValueError, RecursionError):
                await problem(400, "invalid_json", correlation)(scope, receive, send)
                return

        async def replay() -> ASGIMessage:
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        async def safe_send(message: ASGIMessage) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {key.lower() for key, _ in headers}
                headers.extend((key, value) for key, value in [
                    (b"x-correlation-id", correlation.encode()), (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ] if key not in present)
                message["headers"] = headers
            await send(message)

        with telemetry().operation("request") as observation:
            observation.span.set_attribute("harness.request.id", correlation)
            await self.app(scope, replay, safe_send)


@dataclass(frozen=True)
class RequestAccess:
    access: Access
    store: PostgresStore
    repository: SecurityRepository


def create_app(
    config: ServerConfig, store: PostgresStore, identity: JWKSClient, limiter: RateLimiter,
    registry: ToolRegistry, *, verify_database: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if verify_database:
            await SecurityRepository(store).verify_api_role()
        yield

    app = FastAPI(
        title="AI Agent Harness", version="0.9.0", lifespan=lifespan,
        responses={status: {
            "description": "Sanitized application error.",
            "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
        } for status in (400, 401, 403, 404, 405, 408, 409, 410, 412, 413, 422, 428, 429, 500, 503)},
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(config.trusted_hosts))
    app.add_middleware(RequestBoundary)

    @app.exception_handler(HarnessError)
    async def harness_error(request: Request, error: HarnessError) -> JSONResponse:
        logger.warning("api_request_rejected", extra={
            "request_id": request.state.correlation_id, "failure_category": failure_category(error.code),
        })
        statuses = {
            "unauthenticated": 401, "forbidden": 403, "access_denied": 404,
            "precondition_failed": 412, "precondition_required": 428,
            "session_busy": 409, "idempotency_conflict": 409, "workflow_conflict": 409,
            "quota_exceeded": 429, "rate_limit_exceeded": 429,
            "identity_unavailable": 503, "egress_denied": 503, "storage_unavailable": 503,
            "coordination_unavailable": 503, "quota_not_configured": 503, "quota_unavailable": 503,
            "circuit_open": 503,
            "approval_self_forbidden": 403, "approval_conflict": 409, "approval_action_changed": 409,
            "approval_expired": 410,
            "evaluation_self_forbidden": 403, "evaluation_artifact_changed": 409,
            "evaluation_not_ready": 409, "evaluation_conflict": 409,
        }
        code = error.code if error.code in statuses or error.code in {
            "invalid_input", "invalid_precondition", "invalid_pagination", "invalid_output_policy",
            "model_not_allowed", "tool_not_installed",
        } else "request_rejected"
        return problem(statuses.get(code, 400), code, request.state.correlation_id)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid_request(request: Request, error: Exception) -> JSONResponse:
        return problem(422, "validation_failed", request.state.correlation_id)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(error.status_code, "http_error")
        response = problem(error.status_code, code, request.state.correlation_id)
        if error.headers and "Allow" in error.headers:
            response.headers["Allow"] = error.headers["Allow"]
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.error("api_unexpected_error", extra={"request_id": getattr(request.state, "correlation_id", None)})
        return problem(500, "internal_error", getattr(request.state, "correlation_id", str(uuid4())))

    async def authorize(request: Request) -> RequestAccess:
        token = await identity.authenticate(request.headers.get("authorization"))
        repository = SecurityRepository(store.scoped(token.tenant_id))
        access = await repository.access(token)
        await limiter.check(token)
        return RequestAccess(access, repository.store, repository)

    Caller: TypeAlias = Annotated[RequestAccess, Depends(authorize)]

    def build_agent(spec: AgentSpec, caller: RequestAccess, identifier: UUID, version: int) -> Agent:
        if any(key not in config.models for key in (spec.model_key, *spec.fallback_keys)):
            raise HarnessError("model_not_allowed", "Select an installed model key.")
        if not spec.allowed_tools <= caller.access.principal.allowed_tools:
            raise HarnessError("forbidden", "Tool grants cannot be expanded by agent configuration.")
        for name in spec.allowed_tools:
            registry.resolve(name)
        return Agent(
            id=identifier, tenant_id=caller.access.principal.tenant_id, version=version,
            instructions=spec.instructions, selection=config.models[spec.model_key],
            fallbacks=tuple(config.models[key] for key in spec.fallback_keys),
            limits=narrowed_limits(spec.limits, config.limits), retry=config.retry,
            allowed_tools=spec.allowed_tools, result_schema=spec.output_schema,
            approval_policy=spec.approval_policy,
        )

    @app.post("/v1/agents", response_model=Agent, status_code=201)
    async def create_agent(body: AgentCreate, caller: Caller, response: Response) -> Agent:
        caller.access.require("agent:write")
        agent = build_agent(body, caller, body.id, 1)
        await caller.repository.save_agent(caller.access.principal, agent, None)
        response.headers["ETag"] = '"1"'
        return agent

    @app.get("/v1/agents", response_model=Page[Agent])
    async def list_agents(
        caller: Caller, cursor: UUID = ZERO, limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[Agent]:
        caller.access.require("agent:read")
        items = await caller.repository.agents(caller.access.principal, cursor, limit)
        return Page(items=items, next_cursor=str(items[-1].id) if len(items) == limit else None)

    @app.get("/v1/agents/{identifier}", response_model=Agent)
    async def get_agent(
        identifier: UUID, caller: Caller, response: Response, version: Annotated[int | None, Query(ge=1)] = None,
    ) -> Agent:
        caller.access.require("agent:read")
        agent = await caller.repository.agent(caller.access.principal, identifier, version)
        response.headers["ETag"] = f'"{agent.version}"'
        return agent

    @app.patch("/v1/agents/{identifier}", response_model=Agent)
    async def update_agent(
        identifier: UUID, body: AgentSpec, caller: Caller, response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Agent:
        caller.access.require("agent:write")
        expected = etag(if_match)
        agent = build_agent(body, caller, identifier, expected + 1)
        await caller.repository.save_agent(caller.access.principal, agent, expected)
        response.headers["ETag"] = f'"{agent.version}"'
        return agent

    @app.delete("/v1/agents/{identifier}", status_code=204)
    async def delete_agent(
        identifier: UUID, caller: Caller, if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller.access.require("agent:write")
        await caller.repository.disable_agent(caller.access.principal, identifier, etag(if_match))
        return Response(status_code=204)

    def tool_read(name: str, enabled: bool, revision: int) -> ToolRead:
        tool = registry.resolve(name).describe()
        return ToolRead(
            implementation_key=name, revision=revision, enabled=enabled,
            definition=tool.definition, input_schema=tool.input_schema, output_schema=tool.output_schema,
        )

    @app.get("/v1/tool-implementations", response_model=Page[ToolRead])
    async def implementations(caller: Caller) -> Page[ToolRead]:
        caller.access.require("tool:write")
        return Page(items=tuple(tool_read(tool.definition.name, False, 0) for tool in registry.list_installed()))

    @app.post("/v1/tools", response_model=ToolRead, status_code=201)
    async def register_tool(body: ToolRegistration, caller: Caller, response: Response) -> ToolRead:
        caller.access.require("tool:write")
        registry.resolve(body.implementation_key)
        revision = await caller.repository.set_tool(
            caller.access.principal.tenant_id, body.implementation_key, body.enabled, None,
        )
        response.headers["ETag"] = f'"{revision}"'
        return tool_read(body.implementation_key, body.enabled, revision)

    @app.get("/v1/tools", response_model=Page[ToolRead])
    async def tools(caller: Caller) -> Page[ToolRead]:
        caller.access.require("tool:read")
        grants = await caller.repository.tool_grants(caller.access.principal.tenant_id)
        return Page(items=tuple(tool_read(name, state[0], state[1]) for name, state in sorted(grants.items())
                                if name in caller.access.principal.allowed_tools))

    @app.get("/v1/tools/{name}", response_model=ToolRead)
    async def get_tool(name: str, caller: Caller, response: Response) -> ToolRead:
        caller.access.require("tool:read")
        grants = await caller.repository.tool_grants(caller.access.principal.tenant_id)
        if name not in caller.access.principal.allowed_tools or name not in grants:
            raise HarnessError("access_denied", "Tool is not visible.")
        enabled, revision = grants[name]
        response.headers["ETag"] = f'"{revision}"'
        return tool_read(name, enabled, revision)

    @app.patch("/v1/tools/{name}", response_model=ToolRead)
    async def update_tool(
        name: str, body: ToolUpdate, caller: Caller, response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> ToolRead:
        caller.access.require("tool:write")
        registry.resolve(name)
        revision = await caller.repository.set_tool(caller.access.principal.tenant_id, name, body.enabled, etag(if_match))
        response.headers["ETag"] = f'"{revision}"'
        return tool_read(name, body.enabled, revision)

    @app.delete("/v1/tools/{name}", status_code=204)
    async def delete_tool(
        name: str, caller: Caller, if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        caller.access.require("tool:write")
        await caller.repository.set_tool(caller.access.principal.tenant_id, name, False, etag(if_match))
        return Response(status_code=204)

    @app.post("/v1/sessions", response_model=SessionRead, status_code=201)
    async def session(caller: Caller) -> SessionRead:
        caller.access.require("session:write")
        return SessionRead(id=await caller.store.create_session(caller.access.principal))

    @app.get("/v1/sessions/{identifier}", response_model=ContextRead)
    async def context(identifier: UUID, caller: Caller) -> ContextRead:
        caller.access.require("session:read")
        value = await caller.store.context(caller.access.principal, identifier)
        return ContextRead(history=value.history, summary=value.summary, memories=value.memories)

    @app.put("/v1/sessions/{identifier}/memory/{key}", status_code=204)
    async def memory(identifier: UUID, key: str, body: MemoryWrite, caller: Caller) -> Response:
        caller.access.require("memory:write")
        await caller.store.put_memory(caller.access.principal, identifier, key, body.content, body.ttl_seconds)
        return Response(status_code=204)

    @app.post("/v1/executions", response_model=Accepted, status_code=202)
    async def execute(
        body: ExecutionCreate, caller: Caller, response: Response,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ) -> Accepted:
        caller.access.require("execution:create")
        agent = await caller.repository.agent(caller.access.principal, body.agent_id, body.agent_version)
        updates: dict[str, object] = {}
        if body.limits is not None:
            updates["limits"] = narrowed_limits(body.limits, agent.limits)
        if body.output_schema is not None:
            if agent.result_schema is not None and agent.result_schema != body.output_schema:
                raise HarnessError("forbidden", "Agent output policy cannot be replaced.")
            updates["result_schema"] = body.output_schema
        agent = agent.model_copy(update=updates)
        identifier = await caller.store.submit(agent, caller.access.principal, body.session_id, body.input, idempotency_key)
        location = f"/v1/executions/{identifier}"
        response.headers["Location"] = location
        return Accepted(execution_id=identifier, status_url=location)

    @app.get("/v1/executions", response_model=Page[ExecutionRead])
    async def executions(
        caller: Caller, cursor: UUID = ZERO, limit: Annotated[int, Query(ge=1, le=100)] = 50,
        status: Status | None = None,
    ) -> Page[ExecutionRead]:
        caller.access.require("execution:read")
        async with caller.store.transaction() as connection:
            rows = await connection.execute(
                "SELECT checkpoint FROM executions WHERE tenant_id=%s AND owner=%s AND id>%s "
                "AND (%s::text IS NULL OR status=%s) ORDER BY id LIMIT %s",
                (caller.access.principal.tenant_id, caller.access.principal.subject, cursor, status, status, limit),
            )
            from agent_harness.state import ENVELOPE
            items = tuple(ExecutionRead.from_execution(ENVELOPE.validate_python(row["checkpoint"]).execution)
                          for row in await rows.fetchall())
        return Page(items=items, next_cursor=str(items[-1].id) if len(items) == limit else None)

    @app.get("/v1/executions/{identifier}", response_model=ExecutionRead)
    async def execution(identifier: UUID, caller: Caller) -> ExecutionRead:
        caller.access.require("execution:read")
        return ExecutionRead.from_execution((await caller.store.get(caller.access.principal, identifier)).execution)

    @app.post("/v1/executions/{identifier}/cancel", response_model=ExecutionRead, status_code=202)
    async def cancel(identifier: UUID, caller: Caller) -> ExecutionRead:
        caller.access.require("execution:cancel")
        await caller.store.cancel(caller.access.principal, identifier)
        return ExecutionRead.from_execution((await caller.store.get(caller.access.principal, identifier)).execution)

    @app.get("/v1/executions/{identifier}/metrics", response_model=MetricsRead)
    async def execution_stats(identifier: UUID, caller: Caller) -> MetricsRead:
        caller.access.require("execution:read")
        value = (await caller.store.get(caller.access.principal, identifier)).execution
        return MetricsRead.model_validate(execution_metrics(value))

    @app.get("/v1/executions/{identifier}/traces", response_model=Page[TraceRead])
    async def traces(
        identifier: UUID, caller: Caller, cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[TraceRead]:
        caller.access.require("audit:read")
        values = await caller.store.audit(caller.access.principal, identifier, after=cursor, limit=limit)
        return Page(items=tuple(TraceRead(**asdict(item)) for item in values),
                    next_cursor=str(values[-1].sequence) if len(values) == limit else None)

    @app.get("/v1/approvals", response_model=Page[ApprovalRequest])
    async def approvals(
        caller: Caller, cursor: UUID = ZERO, limit: Annotated[int, Query(ge=1, le=100)] = 50,
        status: Annotated[str | None, Query(pattern=r"^(pending|approved|rejected|consumed|expired|cancelled)$")] = None,
    ) -> Page[ApprovalRequest]:
        values = await ApprovalRepository(caller.store).list(caller.access, cursor, limit, status)
        return Page(items=values, next_cursor=str(values[-1].id) if len(values) == limit else None)

    @app.get("/v1/evaluation-rubrics", response_model=EvaluationRubric)
    async def evaluation_rubrics(caller: Caller) -> EvaluationRubric:
        caller.access.require("evaluation:read")
        return EvaluationRubric(dimensions=RUBRIC)

    @app.get("/v1/executions/{identifier}/evaluation-artifact", response_model=ExecutionArtifact)
    async def evaluation_artifact(identifier: UUID, caller: Caller, response: Response) -> ExecutionArtifact:
        value = await EvaluationRepository(caller.store).artifact(caller.access, identifier)
        response.headers["ETag"] = '"' + value.fingerprint() + '"'
        return value

    @app.post("/v1/executions/{identifier}/evaluations", response_model=HumanEvaluation, status_code=201)
    async def human_evaluation(identifier: UUID, body: HumanEvaluationCreate, caller: Caller) -> HumanEvaluation:
        caller.access.require("evaluation:write")
        return await EvaluationRepository(caller.store).create(caller.access, identifier, body)

    @app.get("/v1/executions/{identifier}/evaluations", response_model=Page[HumanEvaluation])
    async def human_evaluations(
        identifier: UUID, caller: Caller, cursor: UUID = ZERO, limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[HumanEvaluation]:
        values = await EvaluationRepository(caller.store).list(caller.access, identifier, cursor, limit)
        return Page(items=values, next_cursor=str(values[-1].id) if len(values) == limit else None)

    @app.get("/v1/approvals/{identifier}", response_model=ApprovalRequest)
    async def approval(identifier: UUID, caller: Caller, response: Response) -> ApprovalRequest:
        value = await ApprovalRepository(caller.store).get(caller.access, identifier)
        response.headers["ETag"] = f'"{value.version}"'
        return value

    @app.post("/v1/approvals/{identifier}/decision", response_model=ApprovalRequest)
    async def decide_approval(
        identifier: UUID, body: ApprovalDecision, caller: Caller, response: Response,
        if_match: Annotated[str | None, Header()] = None,
    ) -> ApprovalRequest:
        caller.access.require("approval:decide")
        expected = etag(if_match)
        repository = ApprovalRepository(caller.store)
        proposed = await repository.get(caller.access, identifier)
        value = await repository.decide(
            caller.access, identifier, body.decision, body.reason, expected, registry.resolve(proposed.tool_name).describe(),
        )
        response.headers["ETag"] = f'"{value.version}"'
        return value

    @app.get("/v1/metrics", response_model=QuotaRead)
    async def tenant_metrics(caller: Caller) -> QuotaRead:
        caller.access.require("metrics:read")
        async with caller.store.transaction() as connection:
            cursor = await connection.execute(
                "SELECT COALESCE(d.executions,0) AS executions,COALESCE(d.tokens,0) AS tokens,"
                "COALESCE(d.cost,0::numeric) AS cost,"
                "(SELECT count(*) FROM quota_reservations WHERE tenant_id=%s AND active) AS active "
                "FROM (SELECT 1) anchor LEFT JOIN quota_days d ON d.tenant_id=%s "
                "AND d.day=(clock_timestamp() AT TIME ZONE 'UTC')::date",
                (caller.access.principal.tenant_id, caller.access.principal.tenant_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HarnessError("quota_unavailable", "Quota metrics are unavailable.")
        return QuotaRead.model_validate(row)

    @app.post("/v1/workflows", response_model=SessionRead, status_code=202)
    async def workflow(body: WorkflowCreate, caller: Caller) -> SessionRead:
        caller.access.require("workflow:create")
        nodes = tuple([WorkflowNode(
            id=node.id, agent=await caller.repository.agent(caller.access.principal, node.agent_id),
            input=node.input, depends_on=node.depends_on, handoff_from=node.handoff_from, condition=node.condition,
        ) for node in body.nodes])
        definition = WorkflowDefinition(nodes=nodes, max_parallelism=body.max_parallelism,
                                        timeout_seconds=body.timeout_seconds)
        return SessionRead(id=await WorkflowStore(caller.store).submit(definition, caller.access.principal))

    @app.get("/v1/workflows/{identifier}", response_model=WorkflowRead)
    async def workflow_status(identifier: UUID, caller: Caller) -> WorkflowRead:
        caller.access.require("workflow:read")
        value = await WorkflowStore(caller.store).get(caller.access.principal, identifier)
        from agent_harness.telemetry import failure_category
        return WorkflowRead(
            id=value.id, status=value.status, created_at=value.created_at, nodes=value.nodes,
            failure_category=failure_category(value.failure.code) if value.failure else None,
        )

    return app


def application() -> FastAPI:
    config = ServerConfig.model_validate_json(Path(os.environ["HARNESS_SERVER_CONFIG"]).read_text(encoding="utf-8"))
    store = PostgresStore(os.environ["HARNESS_DATABASE_URL"])
    client = httpx.AsyncClient(trust_env=False)
    redis = Redis.from_url(os.environ["HARNESS_REDIS_URL"], socket_connect_timeout=3, socket_timeout=3)
    identity = JWKSClient(client, config.oidc)
    registry = ToolRegistry()
    registry.register(calculator_tool())
    app = create_app(config, store, identity, RateLimiter(
        redis, config.tenant_requests_per_minute, config.subject_requests_per_minute,
    ), registry)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def owned_lifespan(value: FastAPI) -> AsyncIterator[None]:
        try:
            async with original(value):
                yield
        finally:
            await client.aclose()
            await redis.aclose()

    app.router.lifespan_context = owned_lifespan
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="OIDC-authenticated harness API; requires migrated restricted database role.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    required = ("HARNESS_DATABASE_URL", "HARNESS_REDIS_URL", "HARNESS_SERVER_CONFIG")
    if any(not os.environ.get(key) for key in required):
        parser.error("Configure " + ", ".join(required))
    try:
        with telemetry_environment():
            server = uvicorn.Server(uvicorn.Config(
                "agent_harness.api:application", factory=True, host=args.host, port=args.port,
                access_log=False, log_level="warning", log_config=None,
            ))
            if sys.platform == "win32":
                asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
            else:
                asyncio.run(server.serve())
    except (ValidationError, HarnessError, OSError):
        parser.exit(1, "API configuration or dependency validation failed.\n")
