CREATE TABLE navigation_sessions (
    session_id uuid PRIMARY KEY,
    tracking_session_id uuid UNIQUE NOT NULL REFERENCES tracking_sessions(session_id) ON DELETE CASCADE,
    owner_id uuid UNIQUE NOT NULL REFERENCES browser_sessions(owner_id) ON DELETE CASCADE,
    route_id uuid NOT NULL REFERENCES saved_routes(route_id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE INDEX navigation_expiry ON navigation_sessions(expires_at);
INSERT INTO mapinator_schema_versions(version) VALUES (4);
