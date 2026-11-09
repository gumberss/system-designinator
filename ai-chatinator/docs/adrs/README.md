# Architecture Decision Records

## ADR-001: Begin with a modular monolith

- **Status:** Accepted
- **Decision:** Keep the synchronous API modules in one .NET application initially, with clear domain/application boundaries and owned persistence. Run background work in a separate worker process.
- **Reason:** The product is early, and conversation, message, orchestration, and usage flows are still evolving. Deploying every capability independently now would add network, deployment, and operational failure modes without demonstrated scale needs.
- **Alternatives:** One deployable per proposed capability; a single unstructured CRUD application.
- **Trade-offs:** Modules can scale independently only after extraction. Enforce module boundaries through project references, contracts, and repository ownership rather than deployment boundaries alone.
- **Revisit when:** A module has independent scaling, compliance, ownership, or availability needs that justify separate deployment.

## ADR-002: Keep conversation and message ownership together

- **Status:** Accepted
- **Decision:** Conversation metadata, ordered messages, share grants, and context summaries belong to one Conversation module and data store.
- **Reason:** Ownership checks, ordered history, sharing, and conversation versioning are tightly coupled.
- **Alternatives:** Separate conversation and message services/tables with cross-service coordination.
- **Trade-offs:** Conversation storage may scale differently from other modules; partitioning by conversation/user and external attachment storage mitigate item and hot-key pressure.

## ADR-003: Separate usage and audit ownership

- **Status:** Accepted
- **Decision:** Credits/usage and audit are independent bounded owners with their own persistence and retention.
- **Reason:** Balance correctness, usage settlement, and append-only audit have different consistency and retention requirements from chat content.
- **Alternatives:** Store all records in the conversation table.
- **Trade-offs:** Cross-module operations are not atomic. Reservations, idempotent settlement, reconciliation, and outbox-driven event delivery are required.

## ADR-004: Use DynamoDB as the AWS system of record; isolate local adapters

- **Status:** Accepted
- **Decision:** AWS persistence targets DynamoDB tables owned per module. Local development uses adapters with the same application contracts; unit tests do not require AWS.
- **Reason:** The requested deployment target is DynamoDB, while local development/testing must not require AWS credentials or resources.
- **Alternatives:** SQLite as the sole persistence technology; accessing AWS in tests.
- **Trade-offs:** Adapters need contract tests that cover the persistence behavior required by each environment.

## ADR-005: Use an outbox and at-least-once messaging

- **Status:** Accepted
- **Decision:** Persist domain changes and outbox entries together where supported; publish via a retrying dispatcher to SNS/SQS. Consumers are idempotent.
- **Reason:** Database mutation and SNS/SQS publication are not one atomic transaction.
- **Alternatives:** Best-effort direct publish; synchronous service chaining.
- **Trade-offs:** Events can be duplicated/delayed; schema versioning, DLQs, alarms, replay, and idempotency are mandatory.

## ADR-006: Put MCP behind a controlled gateway

- **Status:** Accepted
- **Decision:** Orchestration consumes a provider-neutral tool catalog/invocation port; the MCP gateway handles protocol connections and re-checks authorization on every invocation.
- **Reason:** MCP is an integration boundary and should not expose internal repositories or bypass application authorization.
- **Alternatives:** LLM direct access to services; one-off tool calls embedded in orchestration.
- **Trade-offs:** Adds a policy and lifecycle component. Tool schemas, permissions, timeouts, idempotency, and untrusted outputs need explicit handling.
