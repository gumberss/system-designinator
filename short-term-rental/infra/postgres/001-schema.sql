CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE properties (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    city text NOT NULL
);

CREATE TABLE booking_requests (
    idempotency_key uuid PRIMARY KEY,
    property_id uuid NOT NULL REFERENCES properties(id),
    guest_id uuid NOT NULL,
    check_in date NOT NULL,
    check_out date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (check_out > check_in AND check_out - check_in <= 365)
);

CREATE TABLE bookings (
    id uuid PRIMARY KEY,
    idempotency_key uuid NOT NULL UNIQUE REFERENCES booking_requests(idempotency_key),
    property_id uuid NOT NULL REFERENCES properties(id),
    guest_id uuid NOT NULL,
    check_in date NOT NULL,
    check_out date NOT NULL,
    stay daterange GENERATED ALWAYS AS (daterange(check_in, check_out, '[)')) STORED,
    status text NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    cancelled_at timestamptz,
    CHECK (check_out > check_in AND check_out - check_in <= 365),
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL)),
    CONSTRAINT no_overlapping_confirmed_bookings
        EXCLUDE USING gist (property_id WITH =, stay WITH &&)
        WHERE (status = 'confirmed')
);

CREATE INDEX bookings_property_history ON bookings(property_id, created_at DESC);

CREATE TABLE outbox (
    event_id uuid PRIMARY KEY,
    booking_id uuid NOT NULL REFERENCES bookings(id),
    event_type text NOT NULL CHECK (event_type IN ('booking.confirmed', 'booking.cancelled')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_pending ON outbox(created_at) WHERE published_at IS NULL;

CREATE TABLE processed_events (
    event_id uuid PRIMARY KEY,
    event_type text NOT NULL,
    deliveries bigint NOT NULL DEFAULT 1 CHECK (deliveries >= 1),
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- Educational, eventually consistent projection; never used to authorize a booking.
CREATE TABLE booking_statistics (
    property_id uuid PRIMARY KEY REFERENCES properties(id),
    active_bookings bigint NOT NULL DEFAULT 0
);

INSERT INTO properties(id, name, city)
SELECT md5('property-' || i)::uuid, 'Apartamento ' || i,
       (ARRAY['Sao Paulo', 'Rio de Janeiro', 'Salvador', 'Curitiba'])[1 + (i % 4)]
FROM generate_series(1, 2000) AS i;
