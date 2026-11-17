# Phase 2: Real-Time Location & Geospatial Matching

**Status**: STARTING 🚀  
**Objective**: Implement real-time driver location tracking and efficient geospatial matching using Redis GEO  
**Duration**: Est. 7-10 days  
**Performance Target**: Handle 750K GPS updates/second with <100ms matching latency

## Executive Summary

Phase 2 builds upon Phase 1's foundation by adding:
1. **Real-time driver location tracking** - Drivers broadcast GPS updates continuously
2. **Redis GEO-based matching** - Find nearby drivers in <50ms
3. **WebSocket/SignalR communication** - Push updates to drivers and riders
4. **Location-based services** - ETA calculation, driver tracking
5. **Surge pricing foundation** - Prepare demand multiplier logic

---

## Architecture Overview

### Location Tracking Flow

```
┌─────────────────────────────────────────────────────────────┐
│              Driver Mobile App                              │
│  (Every 4 seconds or on demand)                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ POST /api/drivers/{id}/location
                       │ { lat, lng, heading, speed }
                       ▼
┌──────────────────────────────────────────────────────────────┐
│         Location Update API Endpoint                         │
│  (AspNetCore Minimal API - Phase 1B)                         │
└──────────────────────┬───────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
    ┌─────────┐   ┌─────────┐   ┌──────────────┐
    │ Update  │   │ Publish │   │ Broadcast    │
    │ Redis   │   │ Event   │   │ via WebSocket│
    │ GEO     │   │         │   │              │
    └────┬────┘   └────┬────┘   └──────┬───────┘
         │             │               │
         │      ┌──────▼───────┐       │
         │      │ Kafka Topic: │       │
         │      │ location.*   │       │
         │      └──────────────┘       │
         │                              │
         └──────────────┬───────────────┘
                        │
            ┌───────────▼───────────┐
            │  Redis Sorted Set GEO  │
            │  (Within 5km radius)   │
            │                        │
            │ Key: location:active   │
            │ Score: geohash         │
            │ Member: driver-id      │
            └────────────────────────┘
```

### Ride Request Matching Flow (Enhanced)

```
Rider requests ride
    ↓
Search Redis GEO: GEORADIUS location:active lat lng 5km COUNT 10
    ↓
Get 10 nearest drivers (< 50ms)
    ↓
Send DriverOfferSent events via WebSocket (real-time)
    ↓
Driver accepts (first wins via optimistic locking)
    ↓
Create Trip + Broadcast via WebSocket to rider
    ↓
Driver navigates → Continuous location updates
    ↓
Rider sees driver approaching on map (real-time)
    ↓
Driver arrives → Rider gets notification
    ↓
Trip completes
```

---

## Components to Implement

### 1. Location Domain Service (New)
```csharp
public class LocationService
{
    private readonly IRedisClient _redis;
    private readonly IGeocodingService _geocoding;
    
    // Update driver location in Redis GEO
    public async Task UpdateDriverLocationAsync(
        Guid driverId, 
        decimal latitude, 
        decimal longitude)
    
    // Find nearby drivers (returns 10 drivers)
    public async Task<List<DriverLocation>> FindNearbyDriversAsync(
        decimal latitude, 
        decimal longitude, 
        decimal radiusKm = 5m)
    
    // Calculate distance between two points
    public decimal CalculateDistance(
        Location from, 
        Location to)
    
    // Get driver current location
    public async Task<DriverLocation> GetDriverLocationAsync(Guid driverId)
    
    // Bulk update locations (batch processing)
    public async Task BulkUpdateLocationsAsync(
        IEnumerable<DriverLocationUpdate> updates)
}
```

### 2. Matching Service Enhancement
```csharp
public class MatchingService
{
    private readonly ILocationService _locationService;
    private readonly IRideRequestRepository _rideRepository;
    private readonly IDriverOfferRepository _offerRepository;
    
    // Enhanced: uses geospatial search instead of getAllDrivers
    public async Task<List<Driver>> FindMatchingDriversAsync(
        RideRequest rideRequest,
        int maxDistance = 5,
        int maxDrivers = 10)
    {
        // Query Redis GEO for nearby drivers
        var nearbyDrivers = await _locationService
            .FindNearbyDriversAsync(
                rideRequest.PickupLocation.Latitude,
                rideRequest.PickupLocation.Longitude,
                maxDistance);
        
        // Rank by distance, rating, acceptance rate
        return nearbyDrivers
            .OrderBy(d => d.Distance)
            .ThenByDescending(d => d.Driver.AverageRating)
            .Take(maxDrivers)
            .Select(d => d.Driver)
            .ToList();
    }
}
```

### 3. Real-Time Communication Hub (WebSocket/SignalR)
```csharp
public class RideHub : Hub
{
    private readonly IRideRequestRepository _rideRepository;
    private readonly ITripRepository _tripRepository;
    private readonly ILocationService _locationService;
    
    // Rider joins group for ride updates
    public async Task JoinRideAsync(Guid rideId)
    {
        await Groups.AddToGroupAsync(
            Context.ConnectionId, 
            $"ride-{rideId}");
        
        // Send current state to rider
        var ride = await _rideRepository.GetByIdAsync(rideId);
        await Clients.Caller.SendAsync("RideStatusUpdated", ride);
    }
    
    // Driver joins location broadcast group
    public async Task JoinLocationBroadcastAsync(Guid driverId)
    {
        await Groups.AddToGroupAsync(
            Context.ConnectionId, 
            "active-drivers-location");
    }
    
    // Broadcast driver location to all connected riders
    public async Task BroadcastDriverLocation(
        Guid tripId, 
        Guid driverId, 
        DriverLocation location)
    {
        await Clients.Group($"trip-{tripId}")
            .SendAsync("DriverLocationUpdated", location);
    }
    
    // Send driver offer to specific driver
    public async Task SendOfferToDriverAsync(
        Guid driverId, 
        Guid offerId, 
        RideOfferMessage offer)
    {
        await Clients.User(driverId.ToString())
            .SendAsync("NewRideOffer", offer);
    }
}
```

### 4. Surge Pricing Calculator (Foundation)
```csharp
public class SurgePricingService
{
    private readonly IRedisClient _redis;
    private readonly IAnalyticsService _analytics;
    
    // Calculate surge multiplier based on demand/supply
    public async Task<decimal> CalculateSurgeMultiplierAsync(
        Location location,
        DateTime timestamp)
    {
        // 1. Query active ride requests in area
        var activeRequests = await _analytics
            .GetActiveRidesInAreaAsync(location, 2m);
        
        // 2. Query available drivers in area
        var availableDrivers = await _locationService
            .FindNearbyDriversAsync(
                location.Latitude, 
                location.Longitude, 
                2m);
        
        // 3. Calculate ratio: demand / supply
        var ratio = (decimal)activeRequests.Count / 
                    Math.Max(1, availableDrivers.Count);
        
        // 4. Apply surge multiplier
        //    1.0x = normal (supply >= demand)
        //    1.5x = moderate (demand = 1.5x supply)
        //    2.0x = high (demand = 2x supply)
        //    3.0x = surge (demand = 3x+ supply)
        
        return Math.Min(3m, Math.Max(1m, ratio));
    }
    
    // Apply surge to fare
    public decimal ApplyPricingMultiplier(
        decimal baseFare, 
        decimal multiplier)
    {
        return baseFare * multiplier;
    }
}
```

---

## Data Structures in Redis

### 1. Active Drivers Location (Geohash)
```
Key: location:active
Type: Sorted Set
Member: driver:{driverId}
Score: geohash(lat, lng)

Commands:
GEOADD location:active 
  -46.6333 -23.5505 driver:123
  -46.6334 -23.5506 driver:456

GEORADIUS location:active -46.6333 -23.5505 5 km WITHDIST COUNT 10
# Returns:
# 1) "driver:123"
#    "0.15"
# 2) "driver:456"
#    "1.32"
```

### 2. Driver Location Details (Hash)
```
Key: driver:{driverId}:location
Type: Hash
Fields:
  - lat (decimal)
  - lng (decimal)
  - heading (0-359 degrees)
  - speed (km/h)
  - accuracy (meters)
  - updated_at (unix timestamp)
  - trip_id (current trip or null)

Commands:
HSET driver:123:location lat -23.5505 lng -46.6333 updated_at 1234567890
HGETALL driver:123:location
```

### 3. Driver Availability Status
```
Key: driver:{driverId}:status
Type: String
Values: online, offline, on-trip, break

TTL: None (explicit management)

HSET driver:123:status online
EXPIRE driver:123:status 3600  # Auto-logout after 1 hour
```

### 4. Active Rides (for surge pricing)
```
Key: rides:active
Type: Set (members) + location hash

SADD rides:active ride:789
HSET ride:789:info lat lng request_time

# Query active rides near location
GEORADIUS rides:active lat lng 2 km COUNT 100
```

---

## API Endpoints (Phase 2 Additions)

### Location Management
```
PUT /api/v1/drivers/{driverId}/location
Body: { latitude, longitude, heading, speed, accuracy }
Response: { driverId, location, timestamp }

GET /api/v1/drivers/{driverId}/location
Response: { latitude, longitude, heading, speed, lastUpdate }

POST /api/v1/drivers/search-nearby
Body: { latitude, longitude, radiusKm, limit }
Response: [{ driverId, name, location, distance, rating }]
```

### WebSocket Endpoints (SignalR Hubs)
```
connect /hub/rides
  - JoinRideAsync(rideId)
  - LeaveRideAsync(rideId)
  - Events: RideStatusUpdated, DriverLocationUpdated, OfferExpired

connect /hub/drivers
  - JoinLocationAsync(driverId)
  - BroadcastLocationAsync(location)
  - Events: NewRideOffer, RideAssigned, TripStarted

connect /hub/admin
  - GetMetricsAsync()
  - Events: SystemHealthUpdated
```

### Surge Pricing Endpoint
```
GET /api/v1/rides/surge-multiplier
QueryParams: { latitude, longitude }
Response: { multiplier, demand, supply, reason }

GET /api/v1/rides/estimate
Body: { pickupLat, pickupLng, dropoffLat, dropoffLng }
Response: { baseFare, surgeMultiplier, estimatedFare, distance, eta }
```

---

## Implementation Roadmap

### Week 1 (Days 1-5)
1. **Day 1-2**: LocationService implementation
   - Redis GEO integration
   - FindNearbyDrivers query
   - DistanceCalculation
   - Unit tests

2. **Day 3**: Matching Service Enhancement
   - Integrate LocationService
   - Implement ranking algorithm
   - Test concurrent matching

3. **Day 4**: SignalR Hub Setup
   - RideHub and DriverHub
   - Group management
   - Broadcasting

4. **Day 5**: Integration Tests
   - Location updates + GEO queries
   - Real-time updates via WebSocket

### Week 2 (Days 6-10)
6. **Day 6-7**: Surge Pricing Service
   - Multiplier calculation
   - Fare application
   - Analytics queries

7. **Day 8**: API Endpoints
   - Location endpoints
   - Surge pricing endpoints
   - OpenAPI documentation

8. **Day 9**: Performance Testing
   - Load testing (750K updates/sec simulation)
   - Latency testing (<100ms)
   - Stress testing

9. **Day 10**: Documentation & Finalization
   - Architecture diagrams
   - Performance report
   - Deployment guide

---

## Testing Strategy

### Unit Tests
```csharp
[Fact]
public async Task FindNearbyDrivers_Within5km_ReturnsClosestFirst()
{
    // Arrange
    var pickupLocation = new Location(-23.5505m, -46.6333m);
    var driver1 = CreateDriver(lat: -23.5505m, lng: -46.6333m); // 0m
    var driver2 = CreateDriver(lat: -23.5475m, lng: -46.6331m); // 3.3km
    var driver3 = CreateDriver(lat: -23.5325m, lng: -46.6133m); // 18.5km
    
    // Act
    var nearby = await _locationService.FindNearbyDriversAsync(
        pickupLocation.Latitude,
        pickupLocation.Longitude,
        radiusKm: 5);
    
    // Assert
    Assert.Equal(2, nearby.Count); // Only drivers within 5km
    Assert.Equal(driver1.Id, nearby[0].DriverId); // Closest first
    Assert.Equal(driver2.Id, nearby[1].DriverId);
}

[Fact]
public async Task CalculateSurgeMultiplier_HighDemand_ReturnsHighMultiplier()
{
    // Arrange: 10 requests, 2 drivers
    var location = new Location(-23.5505m, -46.6333m);
    
    // Act
    var multiplier = await _surgeService.CalculateSurgeMultiplierAsync(
        location, 
        DateTime.UtcNow);
    
    // Assert
    Assert.True(multiplier >= 2.0m); // 10/2 = 5x demand
}
```

### Integration Tests
```csharp
[Fact]
public async Task RealTimeLocationUpdate_BroadcastsToRiders()
{
    // Arrange
    var driver = CreateDriver();
    var trip = CreateTrip(driverId: driver.Id);
    var rider = CreateRider();
    
    // Act
    await _driver.UpdateLocationAsync(-23.5505m, -46.6333m);
    await Task.Delay(500); // Wait for broadcast
    
    // Assert
    var rider_received = await _riderHub.LastReceivedMessage();
    Assert.NotNull(rider_received.DriverLocation);
    Assert.Equal(driver.Id, rider_received.DriverLocation.DriverId);
}

[Fact]
public async Task GeoSearchPerformance_Under50ms()
{
    // Arrange: 10K drivers in active set
    await PopulateRedisGeoWith10KDrivers();
    
    // Act
    var stopwatch = Stopwatch.StartNew();
    var results = await _locationService.FindNearbyDriversAsync(
        -23.5505m, -46.6333m);
    stopwatch.Stop();
    
    // Assert
    Assert.True(stopwatch.ElapsedMilliseconds < 50);
    Assert.Equal(10, results.Count);
}
```

### Load Testing
```
Target: 750K GPS updates/second
Test Setup:
  - 1M concurrent drivers
  - Each updates location every 4 seconds
  - Peaks: 250K updates/sec average
  - Spikes: 750K updates/sec

Metrics:
  - P50 latency: <10ms
  - P95 latency: <30ms
  - P99 latency: <100ms
  - Success rate: >99.9%
```

---

## Success Criteria

- [ ] FindNearbyDrivers returns results in <50ms
- [ ] Handle 750K GPS updates/second (sustained)
- [ ] Real-time WebSocket updates working
- [ ] Surge pricing algorithm implemented
- [ ] 20+ integration tests passing
- [ ] Load tests passing (750K/sec)
- [ ] OpenAPI documentation complete
- [ ] Performance report generated

---

## Files to Create/Modify

### New Files
```
src/AiUberMarketplace.Domain/
  ├── Location/
  │   ├── DriverLocation.cs         (Value object)
  │   ├── LocationService.cs        (Domain service)
  │   └── ILocationService.cs       (Interface)

src/AiUberMarketplace.Application/
  ├── MatchingService.cs            (Enhanced)
  ├── SurgePricingService.cs
  ├── RealTimeHub.cs                (SignalR)
  └── LocationUpdateHandler.cs      (Event handler)

src/AiUberMarketplace.Infrastructure/
  ├── Redis/
  │   ├── RedisGeoClient.cs
  │   ├── DriverLocationRepository.cs
  │   └── SurgeMultiplierCache.cs

src/AiUberMarketplace.API/
  ├── Hubs/
  │   ├── RideHub.cs
  │   ├── DriverHub.cs
  │   └── HubExtensions.cs
  └── Controllers/
      └── LocationController.cs      (API endpoints)

tests/AiUberMarketplace.Tests/
  ├── LocationServiceTests.cs
  ├── MatchingServiceTests.cs
  ├── SurgePricingTests.cs
  └── RealTimeHubTests.cs
```

### Modified Files
```
src/AiUberMarketplace.Domain/Entities.cs
  - Add CurrentLocation tracking to Driver
  - Add LocationUpdatedEvent

src/AiUberMarketplace.Application/ApplicationServices.cs
  - Inject LocationService into TripApplicationService
  - Use geospatial matching in RequestRideAsync

src/AiUberMarketplace.Infrastructure/DbContext.cs
  - Add DriverLocation entity (if needed for history)
```

---

## Redis Connection Configuration

```csharp
var redis = ConnectionMultiplexer.Connect(
    new ConfigurationOptions
    {
        EndPoints = { "localhost:6379" },
        AbortOnConnectFail = false,
        ConnectTimeout = 5000,
        SyncTimeout = 5000,
        AllowAdmin = true,
        Ssl = false
    });

// Geo commands
var db = redis.GetDatabase();
var geo = db.GeoAdd("location:active", 
    new GeoEntry(-46.6333, -23.5505, "driver:123"));
```

---

## Next: Phase 3 (After Phase 2)

- Payment processing
- Rating & review system
- Advanced matching (surge, preferences)
- Analytics dashboard
- Admin controls

---

**Ready to start Phase 2 implementation!** 🚀

Framework: .NET 10.0  
Language: C# 13  
Technologies: Redis GEO, SignalR, PostgreSQL  
Performance Target: 750K events/sec
