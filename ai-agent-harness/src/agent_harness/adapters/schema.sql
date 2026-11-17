CREATE TABLE sessions (
    tenant_id uuid NOT NULL, id uuid NOT NULL, owner text NOT NULL,
    active_execution_id uuid, next_seq bigint NOT NULL DEFAULT 0,
    summary jsonb, PRIMARY KEY (tenant_id, id)
);
CREATE TABLE executions (
    tenant_id uuid NOT NULL, id uuid NOT NULL, session_id uuid NOT NULL,
    owner text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
    status text NOT NULL, checkpoint jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 0, cancel_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, id), UNIQUE (tenant_id, owner, idempotency_key),
    FOREIGN KEY (tenant_id, session_id) REFERENCES sessions(tenant_id, id)
);
CREATE TABLE jobs (
    tenant_id uuid NOT NULL, execution_id uuid NOT NULL,
    state text NOT NULL DEFAULT 'queued', worker_id uuid,
    fencing_token bigint NOT NULL DEFAULT 0, lease_until timestamptz,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, execution_id),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES executions(tenant_id, id)
);
CREATE INDEX jobs_ready ON jobs(state, available_at, lease_until);
CREATE TABLE messages (
    tenant_id uuid NOT NULL, session_id uuid NOT NULL, seq bigint NOT NULL,
    payload jsonb NOT NULL, PRIMARY KEY (tenant_id, session_id, seq),
    FOREIGN KEY (tenant_id, session_id) REFERENCES sessions(tenant_id, id)
);
CREATE TABLE memories (
    tenant_id uuid NOT NULL, session_id uuid NOT NULL, key text NOT NULL,
    content text NOT NULL, expires_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, session_id, key),
    FOREIGN KEY (tenant_id, session_id) REFERENCES sessions(tenant_id, id)
);
CREATE TABLE execution_events (
    tenant_id uuid NOT NULL, execution_id uuid NOT NULL, version bigint NOT NULL,
    status text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, execution_id, version),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES executions(tenant_id, id)
);
CREATE TABLE workflows (
    tenant_id uuid NOT NULL, id uuid NOT NULL, owner text NOT NULL,
    status text NOT NULL, document jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
