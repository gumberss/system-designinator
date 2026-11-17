ALTER TABLE osm_datasets ADD COLUMN routing_ready boolean NOT NULL DEFAULT false;
CREATE TABLE osm_relations (
    dataset_id uuid NOT NULL REFERENCES osm_datasets ON DELETE CASCADE,
    osm_id bigint NOT NULL,
    members jsonb NOT NULL,
    tags jsonb NOT NULL,
    PRIMARY KEY (dataset_id, osm_id)
);
CREATE TABLE saved_routes (
    route_id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES browser_sessions(owner_id) ON DELETE CASCADE,
    dataset_id uuid NOT NULL REFERENCES osm_datasets,
    result jsonb NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX saved_routes_expiry ON saved_routes(expires_at);
INSERT INTO mapinator_schema_versions(version) VALUES (2);
