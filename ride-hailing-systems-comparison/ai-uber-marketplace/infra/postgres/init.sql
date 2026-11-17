-- PostgreSQL initialization script
-- Runs on first container startup

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "postgis";

-- Create schemas
CREATE SCHEMA IF NOT EXISTS identity;
CREATE SCHEMA IF NOT EXISTS rider;
CREATE SCHEMA IF NOT EXISTS driver;
CREATE SCHEMA IF NOT EXISTS location;
CREATE SCHEMA IF NOT EXISTS trip;
CREATE SCHEMA IF NOT EXISTS payment;
CREATE SCHEMA IF NOT EXISTS fraud;
CREATE SCHEMA IF NOT EXISTS rating;
CREATE SCHEMA IF NOT EXISTS event_store;
CREATE SCHEMA IF NOT EXISTS audit;

-- Event Store table (for transactional outbox pattern)
CREATE TABLE event_store.events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(255) NOT NULL,
    aggregate_id UUID NOT NULL,
    aggregate_type VARCHAR(255) NOT NULL,
    aggregate_version INT NOT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    correlation_id UUID NOT NULL,
    causation_id UUID,
    payload JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP
);

CREATE INDEX idx_events_aggregate_id ON event_store.events(aggregate_id);
CREATE INDEX idx_events_event_type ON event_store.events(event_type);
CREATE INDEX idx_events_created_at ON event_store.events(created_at);
CREATE INDEX idx_events_processed_at ON event_store.events(processed_at);

-- Outbox table (for guaranteed event delivery)
CREATE TABLE event_store.outbox (
    outbox_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES event_store.events(event_id),
    topic VARCHAR(255) NOT NULL,
    partition_key VARCHAR(255),
    payload JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP,
    retry_count INT DEFAULT 0,
    last_error VARCHAR(1000)
);

CREATE INDEX idx_outbox_published_at ON event_store.outbox(published_at);
CREATE INDEX idx_outbox_topic ON event_store.outbox(topic);
CREATE INDEX idx_outbox_created_at ON event_store.outbox(created_at DESC);

-- Audit table
CREATE TABLE audit.event_log (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(255) NOT NULL,
    entity_id UUID NOT NULL,
    action VARCHAR(50) NOT NULL,
    old_values JSONB,
    new_values JSONB,
    user_id UUID,
    ip_address INET,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_entity_id ON audit.event_log(entity_id);
CREATE INDEX idx_audit_created_at ON audit.event_log(created_at DESC);

-- Identity Schema Tables
CREATE TABLE identity.users (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    phone_number VARCHAR(20),
    user_type VARCHAR(50) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_email ON identity.users(email);
CREATE INDEX idx_users_user_type ON identity.users(user_type);

-- Rider Schema Tables
CREATE TABLE rider.rider_profile (
    rider_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES identity.users(user_id),
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    date_of_birth DATE,
    home_address GEOGRAPHY(POINT, 4326),
    work_address GEOGRAPHY(POINT, 4326),
    rider_type VARCHAR(50) DEFAULT 'regular',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_rider_profile_user_id ON rider.rider_profile(user_id);

-- Driver Schema Tables
CREATE TABLE driver.driver_profile (
    driver_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES identity.users(user_id),
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    date_of_birth DATE,
    license_number VARCHAR(50) NOT NULL UNIQUE,
    license_expiry DATE NOT NULL,
    background_check_status VARCHAR(50) DEFAULT 'pending',
    total_trips INT DEFAULT 0,
    total_earnings DECIMAL(15, 2) DEFAULT 0,
    average_rating DECIMAL(3, 2) DEFAULT 0,
    is_available BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_driver_profile_user_id ON driver.driver_profile(user_id);
CREATE INDEX idx_driver_profile_is_available ON driver.driver_profile(is_available);

-- Vehicle Table
CREATE TABLE driver.vehicle (
    vehicle_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_id UUID NOT NULL REFERENCES driver.driver_profile(driver_id),
    license_plate VARCHAR(20) NOT NULL UNIQUE,
    model VARCHAR(255) NOT NULL,
    color VARCHAR(50),
    year INT,
    seats INT DEFAULT 4,
    vehicle_type VARCHAR(50) NOT NULL,
    insurance_expiry DATE,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_vehicle_driver_id ON driver.vehicle(driver_id);

-- Trip Schema Tables
CREATE TABLE trip.ride_request (
    ride_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rider_id UUID NOT NULL REFERENCES rider.rider_profile(rider_id),
    pickup_location GEOGRAPHY(POINT, 4326) NOT NULL,
    dropoff_location GEOGRAPHY(POINT, 4326) NOT NULL,
    pickup_address VARCHAR(500),
    dropoff_address VARCHAR(500),
    preferred_car_types VARCHAR(255),
    estimated_distance DECIMAL(10, 2),
    estimated_duration_seconds INT,
    estimated_fare DECIMAL(15, 2),
    status VARCHAR(50) DEFAULT 'requested',
    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scheduled_for TIMESTAMP,
    cancelled_at TIMESTAMP,
    cancellation_reason VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ride_request_rider_id ON trip.ride_request(rider_id);
CREATE INDEX idx_ride_request_status ON trip.ride_request(status);
CREATE INDEX idx_ride_request_pickup_location ON trip.ride_request USING GIST(pickup_location);

-- Trip Table
CREATE TABLE trip.trip (
    trip_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ride_id UUID NOT NULL UNIQUE REFERENCES trip.ride_request(ride_id),
    driver_id UUID NOT NULL REFERENCES driver.driver_profile(driver_id),
    rider_id UUID NOT NULL REFERENCES rider.rider_profile(rider_id),
    vehicle_id UUID NOT NULL REFERENCES driver.vehicle(vehicle_id),
    status VARCHAR(50) DEFAULT 'assigned',
    pickup_location GEOGRAPHY(POINT, 4326) NOT NULL,
    dropoff_location GEOGRAPHY(POINT, 4326) NOT NULL,
    current_location GEOGRAPHY(POINT, 4326),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    distance_traveled DECIMAL(10, 2),
    duration_seconds INT,
    final_fare DECIMAL(15, 2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_trip_driver_id ON trip.trip(driver_id);
CREATE INDEX idx_trip_rider_id ON trip.trip(rider_id);
CREATE INDEX idx_trip_status ON trip.trip(status);

-- Payment Schema Tables
CREATE TABLE payment.payment (
    payment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trip.trip(trip_id),
    ride_id UUID NOT NULL REFERENCES trip.ride_request(ride_id),
    rider_id UUID NOT NULL REFERENCES rider.rider_profile(rider_id),
    amount DECIMAL(15, 2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'BRL',
    status VARCHAR(50) DEFAULT 'pending',
    payment_method VARCHAR(50),
    transaction_id VARCHAR(255),
    idempotency_key UUID NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_payment_trip_id ON payment.payment(trip_id);
CREATE INDEX idx_payment_rider_id ON payment.payment(rider_id);
CREATE INDEX idx_payment_status ON payment.payment(status);
CREATE INDEX idx_payment_idempotency_key ON payment.payment(idempotency_key);

-- Fraud Schema Tables
CREATE TABLE fraud.fraud_alert (
    alert_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rider_id UUID REFERENCES rider.rider_profile(rider_id),
    driver_id UUID REFERENCES driver.driver_profile(driver_id),
    ride_id UUID REFERENCES trip.ride_request(ride_id),
    severity VARCHAR(50) NOT NULL,
    fraud_score DECIMAL(5, 2),
    reasons JSONB,
    investigation_status VARCHAR(50) DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_fraud_alert_rider_id ON fraud.fraud_alert(rider_id);
CREATE INDEX idx_fraud_alert_severity ON fraud.fraud_alert(severity);

-- Rating Schema Tables
CREATE TABLE rating.rating (
    rating_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trip_id UUID NOT NULL REFERENCES trip.trip(trip_id),
    rated_by VARCHAR(50) NOT NULL,
    rater_id UUID NOT NULL,
    recipient_id UUID NOT NULL,
    score INT NOT NULL CHECK (score >= 1 AND score <= 5),
    comment TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_rating_trip_id ON rating.rating(trip_id);
CREATE INDEX idx_rating_recipient_id ON rating.rating(recipient_id);

-- Grant appropriate permissions
GRANT CONNECT ON DATABASE aiuber_db TO aiuber_user;
GRANT USAGE ON SCHEMA public TO aiuber_user;
GRANT USAGE ON ALL SCHEMAS IN DATABASE aiuber_db TO aiuber_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO aiuber_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO aiuber_user;

-- Create role for specific schemas
ALTER DEFAULT PRIVILEGES IN SCHEMA identity GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA rider GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA driver GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA location GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA trip GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA payment GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA fraud GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA rating GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA event_store GRANT ALL ON TABLES TO aiuber_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT ALL ON TABLES TO aiuber_user;
