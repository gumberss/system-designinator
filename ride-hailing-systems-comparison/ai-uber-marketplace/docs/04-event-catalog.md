# Event Catalog

This document defines all domain events in the AI Uber Marketplace system.

Events are the primary communication mechanism between bounded contexts. Each event represents a fact that happened in the system and cannot be changed retroactively.

## Event Format

```csharp
public class DomainEvent {
    public string EventId { get; set; }  // Unique, idempotent ID
    public string EventType { get; set; }  // Full type name
    public DateTime OccurredAt { get; set; }  // When it happened
    public string AggregateId { get; set; }  // Root entity ID
    public string AggregateType { get; set; }  // Root entity type
    public int AggregateVersion { get; set; }  // Version for ordering
    public string CorrelationId { get; set; }  // Trace across services
    public string CausationId { get; set; }  // What caused this event
    public Dictionary<string, object> Payload { get; set; }
}
```

## Identity Context Events

### UserRegistered
When a new user account is created.

```json
{
  "userId": "user-123",
  "email": "rider@example.com",
  "userType": "rider|driver",
  "registeredAt": "2024-01-15T10:30:00Z",
  "phoneNumber": "+55 11 98765-4321"
}
```

**Published by**: Identity Service  
**Consumed by**: Rider/Driver Services, Notification Service  
**Guaranteed Delivery**: At-least-once  

### UserAuthenticated
When a user logs in successfully (for audit trail).

```json
{
  "userId": "user-123",
  "method": "password|oauth2|biometric",
  "ipAddress": "192.168.1.1",
  "deviceId": "device-abc",
  "authenticatedAt": "2024-01-15T10:31:00Z"
}
```

## Rider Context Events

### RiderRegistered
When a rider completes registration (extends UserRegistered with profile).

```json
{
  "riderId": "rider-123",
  "userId": "user-123",
  "firstName": "João",
  "lastName": "Silva",
  "email": "joao@example.com",
  "phoneNumber": "+55 11 98765-4321",
  "homeAddress": {
    "latitude": -23.5505,
    "longitude": -46.6333,
    "placeId": "place-home"
  },
  "workAddress": null,
  "riderType": "regular|premium",
  "registeredAt": "2024-01-15T10:30:00Z"
}
```

**Published by**: Rider Service  
**Consumed by**: AI Rider Assistant, Analytics, Rating Service  

### RiderProfileUpdated
When rider changes profile settings.

```json
{
  "riderId": "rider-123",
  "preferredPaymentMethod": "credit_card|wallet|cash",
  "riderPreferences": {
    "musicPreference": "quiet|normal|party",
    "talkativeDriver": true,
    "preferredCarTypes": ["economy", "comfort"]
  },
  "updatedAt": "2024-01-15T11:00:00Z"
}
```

## Driver Context Events

### DriverRegistered
When a driver completes registration and verification.

```json
{
  "driverId": "driver-456",
  "userId": "user-456",
  "firstName": "Maria",
  "lastName": "Santos",
  "email": "maria@example.com",
  "phoneNumber": "+55 11 99876-5432",
  "licenseNumber": "ABC123456",
  "licenseExpiry": "2025-12-31",
  "backgroundCheckStatus": "approved|pending|failed",
  "registeredAt": "2024-01-15T14:00:00Z"
}
```

**Published by**: Driver Service  
**Consumed by**: Location Service, Notification Service, Fraud Service  

### VehicleRegistered
When a driver adds a vehicle.

```json
{
  "vehicleId": "vehicle-789",
  "driverId": "driver-456",
  "licensePlate": "ABC1234",
  "model": "Toyota Prius",
  "color": "Silver",
  "year": 2022,
  "seats": 4,
  "insuranceExpiry": "2025-06-30",
  "vehicleType": "economy|comfort|premium",
  "registeredAt": "2024-01-15T14:30:00Z"
}
```

### DriverAvailabilityChanged
When driver goes online/offline or changes availability status.

```json
{
  "driverId": "driver-456",
  "vehicleId": "vehicle-789",
  "status": "online|offline|on_trip|break",
  "location": {
    "latitude": -23.5505,
    "longitude": -46.6333,
    "accuracy": 10.5
  },
  "changedAt": "2024-01-15T15:00:00Z",
  "reason": "user_action|trip_started|trip_ended|battery_low"
}
```

**Published by**: Driver Service  
**Consumed by**: Location Service, Matching Service, Notification Service  

## Location Context Events

### DriverLocationUpdated
**Frequency**: Every 4 seconds per active driver  
**Volume**: ~750,000 events/second  

```json
{
  "driverId": "driver-456",
  "vehicleId": "vehicle-789",
  "latitude": -23.5505,
  "longitude": -46.6333,
  "accuracy": 10.5,
  "heading": 45,
  "speed": 25.5,
  "sequenceNumber": 12345,
  "collectedAt": "2024-01-15T15:00:00Z",
  "processedAt": "2024-01-15T15:00:00.050Z"
}
```

**Published by**: Driver mobile app (via Location Service)  
**Consumed by**: 
- Geospatial Service (index in Redis)
- Location history consumer (archive to Cassandra)
- Fraud Detection (speed/location anomalies)
- Analytics  

**Delivery Guarantee**: At-least-once (deduplication by sequence number)  
**Ordering**: Must maintain order per driver (sequence number)  
**Critical Processing**: <100ms ingestion, <1s indexing  

## Ride Context Events

### RideRequested
When a rider requests a new ride.

```json
{
  "rideId": "ride-001",
  "riderId": "rider-123",
  "pickupLocation": {
    "latitude": -23.5505,
    "longitude": -46.6333,
    "address": "Av Paulista 1000",
    "placeId": "place-pickup"
  },
  "dropoffLocation": {
    "latitude": -23.5500,
    "longitude": -46.6400,
    "address": "Rua Augusta 500",
    "placeId": "place-dropoff"
  },
  "preferredCarTypes": ["economy"],
  "estimatedDistance": 2.5,
  "estimatedDurationSeconds": 600,
  "estimatedFare": 25.50,
  "requestedAt": "2024-01-15T15:10:00Z",
  "scheduledFor": null
}
```

**Published by**: Ride Request Service  
**Consumed by**: Matching Service, ETA Service, Payment Service  

### DriverOfferSent
When matching service sends a ride offer to a driver.

```json
{
  "offerId": "offer-001",
  "rideId": "ride-001",
  "driverId": "driver-456",
  "pickupLocation": { },
  "estimatedPickupTime": 180,
  "estimatedFare": 25.50,
  "sentAt": "2024-01-15T15:10:05Z",
  "expiresAt": "2024-01-15T15:10:25Z"
}
```

**Published by**: Matching Service  
**Consumed by**: Driver notification, Ride tracking  
**TTL**: 20 seconds (must respond or offer expires)  

### OfferAccepted
When a driver accepts a ride offer.

```json
{
  "offerId": "offer-001",
  "rideId": "ride-001",
  "driverId": "driver-456",
  "acceptedAt": "2024-01-15T15:10:10Z"
}
```

**Published by**: Ride Request Service (after API call)  
**Consumed by**: Trip Management, Rider notification, Matching  
**Critical**: Must be idempotent (duplicate acceptances create single trip)  

### OfferRejected
When a driver rejects a ride offer.

```json
{
  "offerId": "offer-001",
  "rideId": "ride-001",
  "driverId": "driver-456",
  "reason": "too_far|low_rating|destination|vehicle_type",
  "rejectedAt": "2024-01-15T15:10:15Z"
}
```

### DriverAssigned
**Critical Event**: Ride now has a driver. Idempotency key prevents duplicates.

```json
{
  "rideId": "ride-001",
  "driverId": "driver-456",
  "vehicleId": "vehicle-789",
  "assignedAt": "2024-01-15T15:10:10Z",
  "idempotencyKey": "ride-001-assign-v1"
}
```

**Published by**: Trip Management Service  
**Consumed by**: Rider notification, Payment Service, Analytics  
**Guarantee**: Exactly once per ride  

### DriverArrivingAtPickup
When driver is near pickup location.

```json
{
  "rideId": "ride-001",
  "driverId": "driver-456",
  "estimatedArrivalSeconds": 30,
  "arrivingAt": "2024-01-15T15:12:00Z"
}
```

### DriverArrivedAtPickup
When driver reaches pickup location.

```json
{
  "rideId": "ride-001",
  "driverId": "driver-456",
  "arrivedAt": "2024-01-15T15:13:00Z"
}
```

### TripStarted
When rider gets in vehicle and trip begins.

```json
{
  "rideId": "ride-001",
  "tripId": "trip-001",
  "driverId": "driver-456",
  "riderId": "rider-123",
  "startedAt": "2024-01-15T15:13:30Z",
  "pickupLocation": { },
  "initialRoute": { }
}
```

**Published by**: Trip Management Service  
**Consumed by**: Payment Service, Fraud Detection, Analytics  

### TripInProgress
Periodic events during active trip for tracking.

```json
{
  "tripId": "trip-001",
  "driverId": "driver-456",
  "riderId": "rider-123",
  "currentLocation": {
    "latitude": -23.5510,
    "longitude": -46.6340
  },
  "distanceTraveledMeters": 500,
  "estimatedTimeRemainingSeconds": 120,
  "progress": "heading_to_destination",
  "updatedAt": "2024-01-15T15:14:00Z"
}
```

**Frequency**: Every 5-10 seconds  
**Published by**: Trip Management (streams events)  

### TripCompleted
When rider exits vehicle and trip ends.

```json
{
  "rideId": "ride-001",
  "tripId": "trip-001",
  "driverId": "driver-456",
  "riderId": "rider-123",
  "completedAt": "2024-01-15T15:18:30Z",
  "dropoffLocation": {
    "latitude": -23.5500,
    "longitude": -46.6400
  },
  "distanceTraveledMeters": 2850,
  "durationSeconds": 510,
  "finalFare": 26.75
}
```

**Published by**: Trip Management Service  
**Consumed by**: Payment Service, Rating Service, Analytics, Fraud  

### RideCancelled
When rider or driver cancels ride before/during trip.

```json
{
  "rideId": "ride-001",
  "cancelledBy": "rider|driver",
  "cancelledAt": "2024-01-15T15:11:00Z",
  "reason": "rider_requested|driver_requested|no_show|emergency",
  "cancellationFee": 5.00
}
```

## Payment Context Events

### PaymentAuthorized
Authorization hold placed on payment method.

```json
{
  "paymentId": "payment-001",
  "rideId": "ride-001",
  "riderId": "rider-123",
  "amount": 26.75,
  "currency": "BRL",
  "paymentMethod": "credit_card",
  "authorizationCode": "AUTH-12345",
  "authorizedAt": "2024-01-15T15:18:35Z",
  "expiresAt": "2024-01-15T16:18:35Z"
}
```

**Published by**: Payment Service  
**Consumed by**: Trip Management, Fraud Detection  

### PaymentCaptured
Final charge captured from rider.

```json
{
  "paymentId": "payment-001",
  "rideId": "ride-001",
  "riderId": "rider-123",
  "amount": 26.75,
  "fare": 20.00,
  "tip": 5.00,
  "discount": 1.25,
  "tax": 2.00,
  "transactionId": "txn-12345",
  "capturedAt": "2024-01-15T15:19:00Z"
}
```

**Published by**: Payment Service  
**Consumed by**: Driver Service (credit), Accounting, Analytics  
**Guarantee**: Idempotent (idempotency key prevents double charges)  

### PaymentFailed
Payment capture failed.

```json
{
  "paymentId": "payment-001",
  "rideId": "ride-001",
  "reason": "insufficient_funds|card_declined|expired|processing_error",
  "failedAt": "2024-01-15T15:19:05Z",
  "retryCount": 0,
  "nextRetryAt": "2024-01-15T16:19:05Z"
}
```

### PaymentRefunded
Refund issued to rider.

```json
{
  "refundId": "refund-001",
  "paymentId": "payment-001",
  "rideId": "ride-001",
  "amount": 26.75,
  "reason": "trip_cancelled|duplicate|merchant_error",
  "refundedAt": "2024-01-15T15:25:00Z"
}
```

## Fraud Context Events

### FraudDetected
Suspicious activity identified.

```json
{
  "fraudAlertId": "alert-001",
  "riderId": "rider-123",
  "rideId": "ride-001",
  "severity": "low|medium|high|critical",
  "reasons": [
    "rapid_location_change",
    "geographic_impossibility",
    "unusual_payment_method"
  ],
  "fraudScore": 0.85,
  "detectedAt": "2024-01-15T15:19:01Z"
}
```

**Published by**: Fraud Detection Service  
**Consumed by**: Operations team, AI Fraud Analyst, Payment Service  

### FraudInvestigationStarted
Investigation case opened.

```json
{
  "investigationId": "inv-001",
  "fraudAlertId": "alert-001",
  "riderId": "rider-123",
  "assignedTo": "investigator-001",
  "startedAt": "2024-01-15T15:20:00Z"
}
```

## Rating Context Events

### RatingSubmitted
Rider/Driver rating after trip completion.

```json
{
  "ratingId": "rating-001",
  "tripId": "trip-001",
  "ratedBy": "rider|driver",
  "ratingGiver": "rider-123|driver-456",
  "ratingRecipient": "driver-456|rider-123",
  "score": 5,
  "comment": "Great driver, clean car",
  "submittedAt": "2024-01-15T15:30:00Z"
}
```

**Published by**: Rating Service  
**Consumed by**: Driver/Rider reputation, Analytics  
**Consistency**: Eventual (ratings updated async)  

## Notification Context Events

### NotificationSent
Notification delivered to user.

```json
{
  "notificationId": "notif-001",
  "userId": "user-123",
  "channel": "websocket|push|sms|email",
  "type": "ride_offered|driver_assigned|driver_arriving",
  "payload": { },
  "sentAt": "2024-01-15T15:10:05Z"
}
```

## Analytics Context Events

### AnalyticsEventRecorded
Aggregated metric snapshot.

```json
{
  "metricId": "metric-001",
  "timestamp": "2024-01-15T15:00:00Z",
  "metricType": "ride_completed|payment_processed|driver_online",
  "count": 150,
  "dimensions": {
    "city": "São Paulo",
    "hour": 15
  }
}
```

## Event Ordering & Idempotency

### Critical Path for Ride Completion
```
RideRequested (1)
  ↓
DriverOfferSent (2+) [one per driver]
  ↓
OfferAccepted (3) [must be idempotent]
  ↓
DriverAssigned (4) [idempotency key: ride-id-version]
  ↓
TripStarted (5)
  ↓
TripInProgress (6+) [periodic, not critical for ordering]
  ↓
TripCompleted (7)
  ↓
PaymentAuthorized (8)
  ↓
PaymentCaptured (9) [idempotency key]
  ↓
RatingSubmitted (10+) [eventual, both rider and driver)
```

### Idempotency Keys
- **OfferAccepted**: `{offerId}` (one offer = one acceptance)
- **DriverAssigned**: `{rideId}-assign-v1` (one assignment per ride)
- **PaymentAuthorized**: `{paymentId}` (idempotent auth)
- **PaymentCaptured**: `{paymentId}-capture` (idempotent capture)

## Event Schema Versioning

When events change, use version suffixes:
- `RideRequested.v1` (original)
- `RideRequested.v2` (added field)
- `RideRequested.v3` (breaking change, old version grandfathered)

Consumers must handle multiple versions in transition period.

## References

- [Event Sourcing Pattern](https://martinfowler.com/eaaDev/EventSourcing.html)
- [Transactional Outbox Pattern](https://microservices.io/patterns/data/transactional-outbox.html)
- [Event Versioning Strategy](https://github.com/EventStoreDB/EventStore/wiki/Event-Versioning)
