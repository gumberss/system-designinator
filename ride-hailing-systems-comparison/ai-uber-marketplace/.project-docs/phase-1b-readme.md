# Phase 1B: Infrastructure & API Layer - README

## Overview

Phase 1B has successfully implemented the infrastructure and API foundation for the AI Uber Marketplace. This layer provides:

- **Database Layer**: EF Core with PostgreSQL
- **REST API**: Complete controller structure with 4 controllers
- **Dependency Injection**: Service registration and middleware pipeline
- **Error Handling**: Global exception middleware
- **API Documentation**: OpenAPI/Swagger integration

## Architecture

```
AiUberMarketplace.Api
├── Program.cs (Application entry point)
├── Controllers/ (HTTP endpoints)
│   ├── DriversController.cs
│   ├── RidesController.cs
│   ├── OffersController.cs
│   └── SurgeController.cs
└── Dtos/ (Data transfer objects)
    └── RideResponseDtos.cs

AiUberMarketplace.Infrastructure
├── Persistence/
│   ├── AiUberMarketplaceDbContext.cs (EF Core configuration)
│   ├── AiUberMarketplaceDbContextFactory.cs (Design-time factory)
│   └── Migrations/ (Database schema)
│       └── 20261111203530_InitialCreate.cs
├── Extensions/
│   └── ServiceCollectionExtensions.cs (DI configuration)
└── Middleware/
    └── ErrorHandlingMiddleware.cs (Global exception handler)
```

## Build & Run

### Prerequisites
- .NET 10.0 SDK
- PostgreSQL 12+ (for database)
- Redis (for caching and location services)

### Building
```bash
cd ai-uber-marketplace
dotnet build
```

### Configuration
Edit `appsettings.json` with your database connection strings:
```json
{
  "ConnectionStrings": {
    "DefaultConnection": "Host=localhost;Port=5432;Database=ai_uber;Username=postgres;Password=postgres;",
    "Redis": "localhost:6379"
  }
}
```

### Running
```bash
cd src/AiUberMarketplace.Api
dotnet run
```

The API will start on:
- HTTP: `http://localhost:5000`
- HTTPS: `https://localhost:5001`
- Swagger UI: `http://localhost:5000` (development only)

## API Endpoints

### Drivers `/api/drivers`
- `GET /{id}` - Get driver profile
- `PUT /{id}/location` - Update driver location
- `GET /{id}/trips` - Get driver trip history
- `POST /{id}/availability` - Toggle driver online/offline

### Rides `/api/rides`
- `POST` - Create new ride request
- `GET /{id}` - Get ride details
- `GET ?riderId={id}` - Get rider's rides
- `PUT /{id}/cancel` - Cancel ride
- `PUT /{id}/complete` - Complete trip

### Offers `/api/offers`
- `GET ?driverId={id}` - Get active offers for driver
- `PUT /{id}/accept` - Accept offer
- `PUT /{id}/reject` - Reject offer

### Surge Pricing `/api/surge`
- `GET /multiplier?latitude={lat}&longitude={lng}` - Get surge multiplier

## Database Schema

The initial migration creates these tables:

**riders** - Rider profiles
- id, email, phone, type, created_at, updated_at

**drivers** - Driver profiles  
- id, email, license_number, is_available, current_location, vehicle_info, rating, trips

**trips** - Individual trip records
- id, rider_id, driver_id, status, pickup_location, dropoff_location, fare, duration

**ride_requests** - Ride request aggregates
- id, rider_id, status, locations, estimated_fare

**payments** - Payment transactions
- id, trip_id, amount, method, status, transaction_id

**offers** - Driver offers for rides
- id, trip_id, driver_id, expires_at, status

**location_history** - GPS tracking
- id, driver_id, latitude, longitude, timestamp

**outbox_events** - Event sourcing
- id, event_type, payload, created_at, processed_at

## Data Flow

```
Client Request
    ↓
[Swagger UI / HTTP Client]
    ↓
DriversController / RidesController / OffersController / SurgeController
    ↓
ErrorHandlingMiddleware (catches exceptions)
    ↓
CorsMiddleware (handles cross-origin requests)
    ↓
[Repository Layer - Phase 1C] (to be implemented)
    ↓
AiUberMarketplaceDbContext
    ↓
PostgreSQL Database
    ↓
Redis Cache (for location/matching services)
```

## Services Placeholder

Controllers currently have commented-out service dependencies that will be implemented in Phase 1C:

```csharp
// TODO: Call LocationService to update driver location in Redis
// TODO: Call MatchingService to find nearby drivers
// TODO: Call SurgePricingService to calculate multiplier
```

These will be wired up when repositories are created.

## Testing

### Build Tests
```bash
dotnet test
```

### Manual API Testing
Use Swagger UI at `http://localhost:5000` (development mode)

Or with curl:
```bash
# Get driver
curl -X GET "http://localhost:5000/api/drivers/{id}"

# Create ride
curl -X POST "http://localhost:5000/api/rides" \
  -H "Content-Type: application/json" \
  -d '{
    "riderId": "00000000-0000-0000-0000-000000000000",
    "pickupLatitude": -23.5505,
    "pickupLongitude": -46.6333,
    "dropoffLatitude": -23.5510,
    "dropoffLongitude": -46.6340,
    "pickupAddress": "Av Paulista, São Paulo",
    "dropoffAddress": "Rua Augusta, São Paulo"
  }'

# Get surge multiplier
curl -X GET "http://localhost:5000/api/surge/multiplier?latitude=-23.5505&longitude=-46.6333"
```

## Known Limitations

The following will be implemented in future phases:

**Phase 1C - Repository Pattern:**
- Repository implementations
- Query logic for all aggregates
- Data persistence

**Phase 1D - Advanced Features:**
- Authentication/Authorization
- Background jobs
- Rate limiting

**Phase 2 - Real-time:**
- SignalR for live updates
- WebSocket integration

**Phase 3 - Performance:**
- Advanced caching strategies
- Query optimization
- Database indexing

## File Structure

```
/src
  /AiUberMarketplace.Api
    Program.cs
    Controllers/
      - DriversController.cs
      - RidesController.cs
      - OffersController.cs
      - SurgeController.cs
    Dtos/
      - RideResponseDtos.cs
    
  /AiUberMarketplace.Infrastructure
    Persistence/
      - AiUberMarketplaceDbContext.cs
      - AiUberMarketplaceDbContextFactory.cs
      - Migrations/
        - 20261111203530_InitialCreate.cs
        - AiUberMarketplaceDbContextModelSnapshot.cs
    Extensions/
      - ServiceCollectionExtensions.cs
    Middleware/
      - ErrorHandlingMiddleware.cs
    Services/
      - (existing: RedisLocationService, etc)

/appsettings.json
```

## Dependencies

**NuGet Packages Added:**
- `Microsoft.EntityFrameworkCore` (9.0.0) - ORM
- `Npgsql.EntityFrameworkCore.PostgreSQL` (9.0.0) - PostgreSQL provider
- `Microsoft.EntityFrameworkCore.Design` (9.0.0) - Migrations
- `Swashbuckle.AspNetCore` (6.0.0) - Swagger UI
- `Microsoft.AspNetCore.OpenApi` (9.0.0) - OpenAPI support

**Existing Packages:**
- `StackExchange.Redis` (2.6.122) - Redis client

## Troubleshooting

### Database Connection Failed
```
Error: Unable to connect to PostgreSQL
Solution: Ensure PostgreSQL is running and connection string is correct
```

### Migration Failed
```
Error: Migration 20261111203530_InitialCreate failed
Solution: Clear database or check schema conflicts
```

### Swagger Not Loading
```
Error: Swagger UI returns 404
Solution: Ensure running in Development environment
```

### Port Already in Use
```
Error: Port 5000/5001 already in use
Solution: Change launchSettings.json or kill existing process
```

## What's Next

**Phase 1C - Repository & Data Access:**
- Create repository implementations
- Add LINQ queries
- Integrate with domain services

**Phase 1B-4 - Integration Tests:**
- Test database operations
- Test API endpoints
- Test service integration

**Phase 1B-5 - Event Sourcing:**
- Implement outbox pattern
- Create event publisher
- Add background worker

## Contributing

When extending Phase 1B:

1. Follow the existing controller/DTO patterns
2. Use async/await for all database operations
3. Add XML documentation to public methods
4. Test locally before committing
5. Update this README if adding new features

## Contact

For issues or questions about Phase 1B, refer to:
- PHASE1B-COMPLETION-REPORT.md
- PHASE1B-ROADMAP.md
- Code comments in controllers

---

**Phase 1B Status:** ✅ Complete  
**Build Status:** ✅ Passing (0 errors)  
**Ready for Phase 1C:** ✅ Yes  
**Date:** 2026-11-11
