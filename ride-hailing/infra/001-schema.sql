CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE passengers (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    email text,
    phone text,
    photo_url text
);

CREATE TABLE drivers (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    category text NOT NULL CHECK (category IN ('standard', 'comfort')),
    online boolean NOT NULL DEFAULT true,
    busy boolean NOT NULL DEFAULT false,
    email text,
    phone text,
    photo_url text,
    vehicle_name text NOT NULL DEFAULT 'Sedan ficticio',
    vehicle_plate text NOT NULL DEFAULT 'LAB-0000'
);

CREATE TABLE trips (
    id uuid PRIMARY KEY,
    passenger_id uuid NOT NULL REFERENCES passengers(id),
    driver_id uuid REFERENCES drivers(id),
    category text NOT NULL CHECK (category IN ('standard', 'comfort')),
    pickup geography(Point, 4326) NOT NULL,
    destination geography(Point, 4326) NOT NULL,
    status text NOT NULL CHECK (status IN
        ('searching', 'assigned', 'arrived', 'in_progress', 'completed', 'cancelled', 'expired')),
    version integer NOT NULL DEFAULT 1,
    fare_cents integer NOT NULL CHECK (fare_cents >= 0),
    match_distance_m double precision,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    assigned_at timestamptz,
    ended_at timestamptz,
    event_sequence bigint NOT NULL DEFAULT 0,
    last_dispatch_at timestamptz,
    search_deadline timestamptz NOT NULL DEFAULT clock_timestamp() + interval '60 seconds',
    CHECK ((status IN ('assigned', 'arrived', 'in_progress', 'completed')) = (driver_id IS NOT NULL)
        OR (status = 'cancelled'))
);
CREATE UNIQUE INDEX one_active_trip_per_passenger ON trips(passenger_id)
    WHERE status IN ('searching', 'assigned', 'arrived', 'in_progress');
CREATE UNIQUE INDEX one_active_trip_per_driver ON trips(driver_id)
    WHERE status IN ('assigned', 'arrived', 'in_progress');
CREATE INDEX searching_trips ON trips(created_at) WHERE status = 'searching';
CREATE INDEX trip_driver_window ON trips(driver_id, assigned_at, ended_at);
CREATE INDEX trip_rider_window ON trips(passenger_id, assigned_at, ended_at);

CREATE TABLE requests (
    passenger_id uuid NOT NULL REFERENCES passengers(id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 100),
    fingerprint text NOT NULL,
    trip_id uuid NOT NULL REFERENCES trips(id),
    PRIMARY KEY (passenger_id, idempotency_key)
);

CREATE TABLE trip_events (
    trip_id uuid NOT NULL REFERENCES trips(id),
    id bigint NOT NULL,
    type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    PRIMARY KEY (trip_id, id)
);
CREATE INDEX unpublished_trip_events ON trip_events(created_at) WHERE published_at IS NULL;

CREATE TABLE driver_location (
    driver_id uuid PRIMARY KEY REFERENCES drivers(id),
    position geography(Point, 4326) NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL
);

CREATE TABLE rider_location (
    rider_id uuid PRIMARY KEY REFERENCES passengers(id),
    position geography(Point, 4326) NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 0),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL
);

CREATE TABLE route_points (
    trip_id uuid NOT NULL REFERENCES trips(id),
    actor text NOT NULL CHECK (actor IN ('driver', 'rider')),
    sequence bigint NOT NULL,
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lng double precision NOT NULL CHECK (lng BETWEEN -180 AND 180),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (trip_id, actor, sequence)
);

INSERT INTO passengers (id, name) VALUES
('10000000-0000-4000-8000-000000000001', 'Ana'),
('10000000-0000-4000-8000-000000000002', 'Bruno'),
('10000000-0000-4000-8000-000000000003', 'Carla');

INSERT INTO drivers (id, name, category) VALUES
('20000000-0000-4000-8000-000000000001', 'Diego', 'standard'),
('20000000-0000-4000-8000-000000000002', 'Elisa', 'standard'),
('20000000-0000-4000-8000-000000000003', 'Fabio', 'comfort'),
('20000000-0000-4000-8000-000000000004', 'Gabriela', 'comfort');

INSERT INTO driver_location (driver_id, position, sequence, observed_at, received_at) VALUES
('20000000-0000-4000-8000-000000000001', ST_SetSRID(ST_Point(-46.6550, -23.5600),4326)::geography, 0, clock_timestamp(), clock_timestamp()),
('20000000-0000-4000-8000-000000000002', ST_SetSRID(ST_Point(-46.6510, -23.5640),4326)::geography, 0, clock_timestamp(), clock_timestamp()),
('20000000-0000-4000-8000-000000000003', ST_SetSRID(ST_Point(-46.6620, -23.5620),4326)::geography, 0, clock_timestamp(), clock_timestamp()),
('20000000-0000-4000-8000-000000000004', ST_SetSRID(ST_Point(-46.6500, -23.5580),4326)::geography, 0, clock_timestamp(), clock_timestamp());
