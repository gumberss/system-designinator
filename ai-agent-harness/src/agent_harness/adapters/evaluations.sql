ALTER TABLE memberships DROP CONSTRAINT memberships_role_check;
ALTER TABLE memberships ADD CONSTRAINT memberships_role_check
    CHECK (role IN ('viewer','operator','designer','admin','approver','evaluator'));
CREATE TABLE human_evaluations (
    tenant_id uuid NOT NULL, id uuid NOT NULL, execution_id uuid NOT NULL,
    document jsonb NOT NULL,
    PRIMARY KEY (tenant_id,id),
    FOREIGN KEY (tenant_id,execution_id) REFERENCES executions(tenant_id,id)
);
CREATE INDEX human_evaluations_execution ON human_evaluations(tenant_id,execution_id,id);
ALTER TABLE human_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_evaluations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON human_evaluations TO harness_api,harness_worker
    USING (tenant_id=nullif(current_setting('harness.tenant_id',true),'')::uuid
        OR pg_has_role(current_user,'harness_worker','member'))
    WITH CHECK (tenant_id=nullif(current_setting('harness.tenant_id',true),'')::uuid
        OR pg_has_role(current_user,'harness_worker','member'));
GRANT SELECT,INSERT ON human_evaluations TO harness_api,harness_worker;
CREATE TRIGGER human_evaluations_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
ON human_evaluations FOR EACH STATEMENT EXECUTE FUNCTION harness_audit_append_only();
