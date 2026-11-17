CREATE TABLE execution_audit (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    execution_id uuid NOT NULL,
    event_key text NOT NULL,
    kind text NOT NULL,
    actor text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, execution_id, event_key),
    FOREIGN KEY (tenant_id, execution_id) REFERENCES executions(tenant_id, id)
);
CREATE INDEX execution_audit_read ON execution_audit(tenant_id, execution_id, sequence);
CREATE FUNCTION harness_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Execution audit is append-only';
END;
$$;
CREATE TRIGGER execution_audit_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
ON execution_audit FOR EACH STATEMENT EXECUTE FUNCTION harness_audit_append_only();
