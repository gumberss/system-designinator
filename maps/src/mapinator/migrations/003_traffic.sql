ALTER TABLE tracking_sessions ADD COLUMN traffic_consent boolean NOT NULL DEFAULT false;

CREATE TABLE traffic_outbox (
    event_id uuid PRIMARY KEY,
    topic text NOT NULL,
    partition_key text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL,
    lease_id uuid,
    lease_until timestamptz
);
CREATE INDEX traffic_outbox_ready ON traffic_outbox(next_attempt);
CREATE INDEX traffic_outbox_expiry ON traffic_outbox(expires_at);

CREATE TABLE traffic_inbox (
    event_id uuid PRIMARY KEY,
    processed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE traffic_offsets (
    session_id uuid PRIMARY KEY,
    sequence bigint NOT NULL,
    observed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE traffic_samples (
    graph_version text NOT NULL,
    edge_id text NOT NULL,
    contributor text NOT NULL,
    bucket timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    speed_mps double precision NOT NULL CHECK(speed_mps >= 1),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(graph_version, edge_id, contributor, bucket)
);
CREATE INDEX traffic_samples_time ON traffic_samples(observed_at);
CREATE TABLE traffic_estimates (
    graph_version text NOT NULL,
    edge_id text NOT NULL,
    speed_mps double precision NOT NULL,
    observed_at timestamptz NOT NULL,
    published_at timestamptz NOT NULL,
    confidence double precision NOT NULL,
    contributors integer NOT NULL,
    congestion text NOT NULL,
    anomaly boolean NOT NULL,
    PRIMARY KEY(graph_version, edge_id)
);
CREATE TABLE traffic_history (
    graph_version text NOT NULL,
    edge_id text NOT NULL,
    bucket timestamptz NOT NULL,
    speed_mps double precision NOT NULL,
    contributors integer NOT NULL,
    PRIMARY KEY(graph_version, edge_id, bucket)
);
CREATE TABLE traffic_dead_letters (
    event_id uuid PRIMARY KEY,
    code text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE traffic_worker_state (
    worker text PRIMARY KEY,
    heartbeat timestamptz NOT NULL,
    lag_s double precision NOT NULL
);
INSERT INTO mapinator_schema_versions(version) VALUES(3);
