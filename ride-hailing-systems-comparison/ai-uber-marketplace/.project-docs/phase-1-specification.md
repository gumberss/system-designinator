# Phase 1: Core Ride Lifecycle

**Status**: IN PROGRESS  
**Objective**: Implement the first complete vertical slice - a rider can request and complete a ride  
**Duration**: 2-4 weeks  

## Executive Summary

Phase 1 builds the first **end-to-end working feature**: a rider requests a ride, drivers nearby receive offers, a driver accepts, gets assigned, and completes the trip.

**Key Focus Areas**:
- ✅ Transactional correctness (prevent duplicate assignments)
- ✅ Concurrency safety (race condition testing)
- ✅ Idempotent operations (safe retries)
- ✅ Event publishing (foundation for async processing)
- ✅ 80%+ test coverage
- ✅ Observable (logging, tracing, metrics)

---

## Architecture: Phase 1 Ride Lifecycle

### High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Rider Requests Ride                         │
│  POST /api/rides/request                                            │
│  Body: { pickupLat, pickupLng, dropoffLat, dropoffLng }            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Ride Request Received                             │
│  - Create RideRequest aggregate                                     │
│  - Publish RideRequested event                                      │
│  - Persist to PostgreSQL                                            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Query Redis GEO for Nearby Drivers                      │
│  - Query within 5km radius                                          │
│  - Get top 10 drivers by distance                                   │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│         Send Driver Offers (in parallel)                            │
│  - Create DriverOffer for each driver                               │
│  - Offer expires in 20 seconds                                      │
│  - Publish DriverOfferSent event                                    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                    ┌──────┴──────┐
                    │             │
                    ▼             ▼
         [Driver 1 accepts]  [Driver 2 rejects]
                    │             │
                    └──────┬──────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│         Atomic Driver Assignment (First Accept Wins)                │
│  - Update RideRequest: status = DRIVER_ASSIGNED                    │
│  - Create Trip aggregate                                            │
│  - Publish DriverAssigned event                                     │
│  - Prevent duplicate assignments (optimistic locking)               │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│         Driver Arrives at Pickup Location                           │
│  - Update Trip: status = DRIVER_ARRIVED                            │
│  - Publish DriverArrivedAtPickup event                             │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│         Trip Starts (Rider boards)                                  │
│  - Update Trip: status = IN_PROGRESS                               │
│  - Record startedAt timestamp                                       │
│  - Publish TripStarted event                                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│         Trip Completes (Rider gets off)                             │
│  - Update Trip: status = COMPLETED                                 │
│  - Calculate fare                                                   │
│  - Record completedAt, duration, distance                           │
│  - Publish TripCompleted event                                      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
                      ✅ SUCCESS
```

### Domain Model (Phase 1)

```
Identity Bounded Context:
├─ User (aggregate root)
│  ├─ UserId (value object)
│  ├─ Email
│  ├─ PhoneNumber
│  └─ UserType (rider | driver)

Rider Bounded Context:
├─ Rider (aggregate root)
│  ├─ RiderId
│  ├─ UserId (FK)
│  ├─ FirstName, LastName
│  └─ RiderType (regular | premium)

Driver Bounded Context:
├─ Driver (aggregate root)
│  ├─ DriverId
│  ├─ UserId (FK)
│  ├─ LicenseNumber
│  ├─ IsAvailable (online/offline)
│  └─ Vehicle (value object)
│     ├─ VehicleId
│     ├─ LicensePlate
│     ├─ Model
│     └─ VehicleType

Location Bounded Context:
├─ DriverLocation (value object)
│  ├─ DriverId
│  ├─ Latitude, Longitude
│  └─ UpdatedAt

Trip Bounded Context:
├─ RideRequest (aggregate root)
│  ├─ RideId
│  ├─ RiderId
│  ├─ PickupLocation (lat, lng)
│  ├─ DropoffLocation (lat, lng)
│  ├─ Status (requested | searching | assigned | completed | cancelled)
│  ├─ EstimatedFare
│  └─ CreatedAt

├─ Trip (aggregate root)
│  ├─ TripId
│  ├─ RideId (FK)
│  ├─ DriverId (FK)
│  ├─ RiderId (FK)
│  ├─ VehicleId (FK)
│  ├─ Status (assigned | arriving | arrived | in_progress | completed)
│  ├─ StartedAt
│  ├─ CompletedAt
│  ├─ FinalFare
│  └─ RowVersion (optimistic locking)

├─ DriverOffer (aggregate root)
│  ├─ OfferId
│  ├─ RideId
│  ├─ DriverId
│  ├─ Status (sent | accepted | rejected | expired)
│  ├─ ExpiresAt
│  └─ CreatedAt
```

### Concurrency & Safety Guarantees

#### Problem: Race Condition in Driver Assignment

```
Timeline:
T0: Driver A accepts offer
T1: Driver B accepts offer  (same ride)
T2: Both update database

Result: Two drivers assigned to same ride! ❌
```

#### Solution: Optimistic Locking

```csharp
public class RideRequest
{
    [Timestamp]  // EF Core optimistic locking
    public byte[] RowVersion { get; set; }
}

// When assigning driver:
var rideRequest = await context.RideRequests
    .FirstAsync(r => r.RideId == rideId);

rideRequest.Status = RideRequestStatus.DriverAssigned;
rideRequest.AssignedDriverId = driverId;

try
{
    await context.SaveChangesAsync();  // Throws if RowVersion changed
}
catch (DbUpdateConcurrencyException)
{
    return AlreadyAssignedError();  // Another driver won the race
}
```

#### Solution: Idempotency Keys

```csharp
public class AcceptOfferRequest
{
    public Guid OfferId { get; set; }
    public Guid IdempotencyKey { get; set; }  // UUID from client
}

// In handler:
var existingResult = await cache.Get($"idempotency:{idempotencyKey}");
if (existingResult != null)
    return existingResult;  // Return cached result

// ... process ...

await cache.Set($"idempotency:{idempotencyKey}", result, ttl: 24h);
return result;
```

---

## API Contracts (Phase 1)

### 1. Register Rider

```
POST /api/riders/register
Content-Type: application/json

{
  "email": "joao@example.com",
  "firstName": "João",
  "lastName": "Silva",
  "phoneNumber": "+55 11 98765-4321",
  "riderType": "regular"
}

Response 201:
{
  "riderId": "550e8400-e29b-41d4-a716-446655440000",
  "email": "joao@example.com",
  "createdAt": "2024-01-15T10:30:00Z"
}
```

### 2. Register Driver

```
POST /api/drivers/register
Content-Type: application/json

{
  "email": "maria@example.com",
  "firstName": "Maria",
  "lastName": "Santos",
  "phoneNumber": "+55 11 99876-5432",
  "licenseNumber": "ABC123456",
  "licenseExpiry": "2025-12-31",
  "vehicle": {
    "licensePlate": "ABC1234",
    "model": "Toyota Prius",
    "year": 2022,
    "vehicleType": "economy"
  }
}

Response 201:
{
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "email": "maria@example.com",
  "createdAt": "2024-01-15T10:31:00Z"
}
```

### 3. Update Driver Availability

```
PUT /api/drivers/{driverId}/availability
Content-Type: application/json

{
  "isAvailable": true,
  "latitude": -23.5505,
  "longitude": -46.6333
}

Response 200:
{
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "isAvailable": true,
  "updatedAt": "2024-01-15T10:32:00Z"
}
```

### 4. Request Ride

```
POST /api/rides/request
Content-Type: application/json

{
  "riderId": "550e8400-e29b-41d4-a716-446655440000",
  "pickupLatitude": -23.5505,
  "pickupLongitude": -46.6333,
  "pickupAddress": "Av Paulista 1000",
  "dropoffLatitude": -23.5500,
  "dropoffLongitude": -46.6400,
  "dropoffAddress": "Rua Augusta 500",
  "preferredCarTypes": ["economy"]
}

Response 201:
{
  "rideId": "770e8400-e29b-41d4-a716-446655440000",
  "status": "requested",
  "estimatedFare": 25.50,
  "createdAt": "2024-01-15T10:33:00Z"
}
```

### 5. Accept Driver Offer

```
POST /api/rides/{rideId}/accept
Content-Type: application/json

{
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "offerId": "880e8400-e29b-41d4-a716-446655440000",
  "idempotencyKey": "550e8400-e29b-41d4-a716-446655440111"
}

Response 200:
{
  "rideId": "770e8400-e29b-41d4-a716-446655440000",
  "tripId": "990e8400-e29b-41d4-a716-446655440000",
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "status": "assigned",
  "driverArrivalEta": 180
}
```

### 6. Get Ride Status

```
GET /api/rides/{rideId}

Response 200:
{
  "rideId": "770e8400-e29b-41d4-a716-446655440000",
  "status": "assigned",
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "driverName": "Maria Santos",
  "driverLocation": {
    "latitude": -23.5510,
    "longitude": -46.6340,
    "updatedAt": "2024-01-15T10:33:10Z"
  },
  "currentEta": 120,
  "estimatedFare": 25.50
}
```

### 7. Start Trip

```
POST /api/trips/{tripId}/start
Content-Type: application/json

{
  "driverId": "660e8400-e29b-41d4-a716-446655440000"
}

Response 200:
{
  "tripId": "990e8400-e29b-41d4-a716-446655440000",
  "status": "in_progress",
  "startedAt": "2024-01-15T10:35:00Z"
}
```

### 8. Complete Trip

```
POST /api/trips/{tripId}/complete
Content-Type: application/json

{
  "driverId": "660e8400-e29b-41d4-a716-446655440000",
  "distanceTraveled": 2850,
  "durationSeconds": 510,
  "finalFare": 26.75
}

Response 200:
{
  "tripId": "990e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "completedAt": "2024-01-15T10:43:30Z",
  "finalFare": 26.75,
  "duration": "8 minutes 30 seconds"
}
```

---

## Events Emitted (Phase 1)

### Event Publishing Flow

Each transaction publishes events via **Transactional Outbox** pattern:

```csharp
// 1. Create ride request
var ride = new RideRequest { ... };
context.RideRequests.Add(ride);

// 2. Create outbox event
var @event = new OutboxEvent
{
    EventId = Guid.NewGuid(),
    AggregateId = ride.RideId,
    EventType = "RideRequested",
    Payload = JsonSerializer.Serialize(new {
        rideId = ride.RideId,
        riderId = ride.RiderId,
        pickupLat = ride.PickupLatitude,
        pickupLng = ride.PickupLongitude,
        requestedAt = DateTime.UtcNow
    }),
    CreatedAt = DateTime.UtcNow,
    Published = false
};
context.OutboxEvents.Add(@event);

// 3. Atomic commit
await context.SaveChangesAsync();

// Separate process publishes to Kafka:
// for each unpublished event:
//   await kafka.PublishAsync(topic, @event.Payload);
//   @event.Published = true;
//   await context.SaveChangesAsync();
```

### Events in Phase 1

1. **RideRequested** - Rider creates ride request
   - Topic: `ride-requested`
   - Consumers: (none in Phase 1, foundation for Phase 3)

2. **DriverOfferSent** - Matching sends offer to driver
   - Topic: `driver-offer-sent`
   - Consumers: (none in Phase 1)

3. **OfferAccepted** - Driver accepts offer
   - Topic: `offer-accepted`
   - Consumers: (none in Phase 1)

4. **DriverAssigned** - Trip manager confirms assignment
   - Topic: `driver-assigned`
   - Consumers: Notification (Phase 4)

5. **DriverArrivedAtPickup** - Driver reaches pickup
   - Topic: `driver-arrived`
   - Consumers: Notification (Phase 4)

6. **TripStarted** - Rider boards vehicle
   - Topic: `trip-started`
   - Consumers: Payment (Phase 6), Analytics

7. **TripCompleted** - Trip finishes
   - Topic: `trip-completed`
   - Consumers: Payment (Phase 6), Rating (Phase 6)

---

## Testing Strategy

### Unit Tests
- Aggregate root creation and state transitions
- Value object equality
- Business rule validation

### Integration Tests
- API endpoints + database
- Event publishing to outbox
- Concurrency/race condition scenarios

### Concurrency Tests
```csharp
[Fact]
public async Task AcceptOffer_WhenTwoDriversAcceptSame_OnlyOneAssigned()
{
    // Arrange
    var ride = await CreateRideRequest();
    var driver1 = await CreateDriver();
    var driver2 = await CreateDriver();

    // Act
    var task1 = rideService.AcceptOfferAsync(ride.Id, driver1.Id);
    var task2 = rideService.AcceptOfferAsync(ride.Id, driver2.Id);
    
    await Task.WhenAll(task1, task2);

    // Assert
    var result1 = await task1;
    var result2 = await task2;
    
    Assert.True(result1.IsSuccess || result2.IsSuccess);  // One wins
    Assert.False(result1.IsSuccess && result2.IsSuccess); // Not both
    
    var assignedTrip = await context.Trips
        .Where(t => t.RideId == ride.Id)
        .ToListAsync();
    
    Assert.Single(assignedTrip);  // Only one trip created
}
```

---

## Acceptance Criteria

### Functional Requirements
- [ ] Rider can register
- [ ] Driver can register with vehicle
- [ ] Driver can go online/offline
- [ ] Rider can request ride
- [ ] Nearby drivers receive offers
- [ ] Driver can accept/reject offer
- [ ] First accepting driver gets assigned (atomically)
- [ ] Trip moves through state machine: assigned → arriving → arrived → in_progress → completed
- [ ] Ride completion calculates fare

### Non-Functional Requirements
- [ ] No duplicate driver assignments (concurrency-safe)
- [ ] Idempotent accept operations (safe retries)
- [ ] 80%+ test coverage (unit + integration)
- [ ] All state transitions logged
- [ ] Metrics exposed (latency, success rate)
- [ ] Distributed tracing enabled
- [ ] Database queries optimized (proper indexes)

### Observability
- [ ] Structured logs with correlation ID
- [ ] Distributed tracing in Jaeger
- [ ] Prometheus metrics:
  - `ride_requests_total` (counter)
  - `ride_acceptance_latency_ms` (histogram)
  - `driver_assignment_errors_total` (counter)
  - `concurrent_active_rides` (gauge)

---

## Implementation Phases

### Phase 1A: Domain Layer (2 days)
- [ ] Domain model entities
- [ ] Aggregates and value objects
- [ ] Domain events
- [ ] Repository interfaces
- [ ] Unit tests

### Phase 1B: Application Layer (3 days)
- [ ] Application services
- [ ] DTOs and mappers
- [ ] Error handling
- [ ] Event publishing logic
- [ ] Application tests

### Phase 1C: Infrastructure Layer (3 days)
- [ ] Entity Framework Core configuration
- [ ] Repository implementations
- [ ] Database migrations
- [ ] Outbox event publisher
- [ ] Integration tests

### Phase 1D: API Layer (2 days)
- [ ] REST controllers
- [ ] Request/response validation
- [ ] Error responses (RFC 7807)
- [ ] API documentation
- [ ] End-to-end tests

### Phase 1E: Observability (1 day)
- [ ] Structured logging
- [ ] OpenTelemetry instrumentation
- [ ] Prometheus metrics
- [ ] Health checks

### Phase 1F: Testing & Polish (2 days)
- [ ] Concurrency tests
- [ ] Load testing (basic)
- [ ] Code coverage > 80%
- [ ] Documentation
- [ ] Code review

---

## Definition of Done (Phase 1)

- ✅ All acceptance criteria met
- ✅ 80%+ code coverage
- ✅ All tests green locally
- ✅ Docker Compose environment works
- ✅ No warnings in build
- ✅ API documentation complete
- ✅ Load tested (1,000 concurrent rides)
- ✅ Peer review approved
- ✅ Performance benchmarks baseline established
- ✅ Ready for Phase 2 (geospatial)

---

## Success Metrics

By end of Phase 1, system must handle:

| Metric | Target | Measurement |
|--------|--------|-------------|
| Ride Request Latency (p95) | < 200ms | API response time |
| Driver Assignment Latency (p95) | < 500ms | Event to assignment |
| Concurrency Test | No duplicates | 1000 concurrent accepts |
| Test Coverage | > 80% | Code coverage |
| Build Time | < 30s | dotnet build |
| Docker Compose Startup | < 30s | All health checks pass |

---

**Next**: Implementation begins with Domain Layer 🔨
