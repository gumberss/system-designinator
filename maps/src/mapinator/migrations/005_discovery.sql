CREATE TABLE discovery_conversations (
    conversation_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES browser_sessions(owner_id) ON DELETE CASCADE,
    criteria jsonb NOT NULL CHECK (jsonb_typeof(criteria) = 'object'),
    revision bigint NOT NULL CHECK (revision >= 1),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > created_at)
);
CREATE INDEX discovery_conversations_owner_expiry
    ON discovery_conversations(owner_id, expires_at);
CREATE INDEX discovery_conversations_expiry ON discovery_conversations(expires_at);

INSERT INTO mapinator_schema_versions(version) VALUES (5);
