DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='harness_api') THEN
        CREATE ROLE harness_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='harness_worker') THEN
        CREATE ROLE harness_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END;
$$;
CREATE TABLE memberships (
    tenant_id uuid NOT NULL, subject text NOT NULL,
    role text NOT NULL CHECK (role IN ('viewer','operator','designer','admin')),
    active boolean NOT NULL DEFAULT true,
    allowed_agents uuid[] NOT NULL DEFAULT '{}', allowed_tools text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE agent_catalog (
    tenant_id uuid NOT NULL, id uuid NOT NULL, version integer NOT NULL,
    enabled boolean NOT NULL DEFAULT true, document jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id, version)
);
CREATE TABLE tool_catalog (
    tenant_id uuid NOT NULL, name text NOT NULL, revision integer NOT NULL DEFAULT 1,
    enabled boolean NOT NULL DEFAULT true, PRIMARY KEY (tenant_id, name)
);
CREATE TABLE quota_policy (
    tenant_id uuid PRIMARY KEY,
    max_active integer NOT NULL CHECK (max_active>0),
    max_daily_executions integer NOT NULL CHECK (max_daily_executions>0),
    max_daily_tokens bigint NOT NULL CHECK (max_daily_tokens>0),
    max_daily_cost numeric NOT NULL CHECK (max_daily_cost>0)
);
CREATE TABLE quota_days (
    tenant_id uuid NOT NULL, day date NOT NULL,
    executions bigint NOT NULL DEFAULT 0, tokens bigint NOT NULL DEFAULT 0,
    cost numeric NOT NULL DEFAULT 0, PRIMARY KEY (tenant_id,day)
);
CREATE TABLE quota_reservations (
    tenant_id uuid NOT NULL, execution_id uuid NOT NULL, day date NOT NULL,
    tokens bigint NOT NULL, cost numeric NOT NULL, active boolean NOT NULL DEFAULT true,
    usage_complete boolean NOT NULL DEFAULT false,
    PRIMARY KEY (tenant_id,execution_id),
    FOREIGN KEY (tenant_id,execution_id) REFERENCES executions(tenant_id,id)
);
CREATE INDEX quota_active ON quota_reservations(tenant_id) WHERE active;
DO $$
DECLARE target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'sessions','executions','jobs','messages','memories','execution_events','execution_audit',
        'workflows','memberships','agent_catalog','tool_catalog','quota_policy','quota_days','quota_reservations'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I TO harness_api,harness_worker '
            'USING (tenant_id=nullif(current_setting(''harness.tenant_id'',true),'''')::uuid '
            'OR pg_has_role(current_user,''harness_worker'',''member'')) '
            'WITH CHECK (tenant_id=nullif(current_setting(''harness.tenant_id'',true),'''')::uuid '
            'OR pg_has_role(current_user,''harness_worker'',''member''))', target
        );
    END LOOP;
END;
$$;
GRANT USAGE ON SCHEMA public TO harness_api,harness_worker;
GRANT SELECT ON harness_schema_migrations,memberships,quota_policy TO harness_api,harness_worker;
GRANT UPDATE(tenant_id) ON quota_policy TO harness_api,harness_worker;
GRANT SELECT,INSERT,UPDATE ON sessions,executions,jobs,messages,memories,execution_events,
    workflows,agent_catalog,tool_catalog,quota_days,quota_reservations TO harness_api,harness_worker;
GRANT SELECT,INSERT ON execution_audit TO harness_api,harness_worker;
GRANT USAGE,SELECT ON SEQUENCE execution_audit_sequence_seq TO harness_api,harness_worker;
