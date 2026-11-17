CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS mapinator_schema_versions (
    version integer PRIMARY KEY,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE browser_sessions (
    token_hash text PRIMARY KEY,
    owner_id uuid UNIQUE NOT NULL,
    csrf_token text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX browser_sessions_expiry ON browser_sessions(expires_at);

CREATE TABLE tracking_sessions (
    session_id uuid PRIMARY KEY,
    owner_id uuid UNIQUE NOT NULL REFERENCES browser_sessions(owner_id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);

CREATE TABLE current_locations (
    session_id uuid PRIMARY KEY REFERENCES tracking_sessions(session_id) ON DELETE CASCADE,
    event_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    observed_at timestamptz NOT NULL,
    latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    accuracy_m double precision NOT NULL CHECK (accuracy_m >= 0),
    heading_deg double precision CHECK (heading_deg >= 0 AND heading_deg < 360),
    speed_mps double precision CHECK (speed_mps >= 0),
    expires_at timestamptz NOT NULL
);
CREATE INDEX current_locations_expiry ON current_locations(expires_at);

CREATE TABLE osm_datasets (
    dataset_id uuid PRIMARY KEY,
    checksum text UNIQUE NOT NULL,
    source_url text NOT NULL,
    imported_at timestamptz NOT NULL,
    node_count integer NOT NULL,
    way_count integer NOT NULL,
    place_count integer NOT NULL,
    bounds geometry(Polygon, 4326) NOT NULL,
    active boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX single_active_dataset ON osm_datasets(active) WHERE active;

CREATE TABLE osm_nodes (
    dataset_id uuid NOT NULL REFERENCES osm_datasets ON DELETE CASCADE,
    osm_id bigint NOT NULL,
    location geometry(Point, 4326) NOT NULL,
    tags jsonb NOT NULL,
    PRIMARY KEY (dataset_id, osm_id)
);

CREATE TABLE osm_ways (
    dataset_id uuid NOT NULL REFERENCES osm_datasets ON DELETE CASCADE,
    osm_id bigint NOT NULL,
    node_ids bigint[] NOT NULL,
    geometry geometry(Geometry, 4326) NOT NULL,
    tags jsonb NOT NULL,
    PRIMARY KEY (dataset_id, osm_id)
);
CREATE INDEX osm_ways_geometry ON osm_ways USING gist(geometry);

CREATE TABLE places (
    dataset_id uuid NOT NULL REFERENCES osm_datasets ON DELETE CASCADE,
    source_id text NOT NULL,
    name text NOT NULL,
    category text,
    address jsonb NOT NULL,
    tags jsonb NOT NULL,
    location_kind text NOT NULL,
    location geometry(Point, 4326) NOT NULL,
    search_text tsvector GENERATED ALWAYS AS (to_tsvector('simple', name)) STORED,
    PRIMARY KEY (dataset_id, source_id)
);
CREATE INDEX places_location ON places USING gist(location);
CREATE INDEX places_search ON places USING gin(search_text);

INSERT INTO mapinator_schema_versions(version) VALUES (1);
