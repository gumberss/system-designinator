ALTER TABLE memberships DROP CONSTRAINT memberships_role_check;
ALTER TABLE memberships ADD CONSTRAINT memberships_role_check
    CHECK (role IN ('viewer','operator','designer','admin','approver'));
CREATE TABLE approvals (
    tenant_id uuid NOT NULL, id uuid NOT NULL, execution_id uuid NOT NULL,
    invocation_id uuid NOT NULL, requested_by text NOT NULL,
    agent_id uuid NOT NULL, tool_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','approved','rejected','consumed','expired','cancelled')),
    expires_at timestamptz NOT NULL, version bigint NOT NULL, document jsonb NOT NULL,
    PRIMARY KEY (tenant_id,id), UNIQUE (tenant_id,execution_id,invocation_id),
    FOREIGN KEY (tenant_id,execution_id) REFERENCES executions(tenant_id,id)
);
CREATE INDEX approvals_inbox ON approvals(tenant_id,status,id);
ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON approvals TO harness_api,harness_worker
    USING (tenant_id=nullif(current_setting('harness.tenant_id',true),'')::uuid
        OR pg_has_role(current_user,'harness_worker','member'))
    WITH CHECK (tenant_id=nullif(current_setting('harness.tenant_id',true),'')::uuid
        OR pg_has_role(current_user,'harness_worker','member'));
GRANT SELECT,INSERT,UPDATE ON approvals TO harness_api,harness_worker;
