# Phase 1A → Phase 1B: Next Steps

**Current Status**: Phase 1A (Domain & Application Layer) - ✅ COMPLETE

## What's Ready

✅ Domain Layer fully implemented with 8 entities and 5 aggregates  
✅ Application Services with all use cases  
✅ 54 comprehensive unit tests (100% pass rate)  
✅ Event sourcing foundation ready  
✅ Repository pattern interfaces defined  

## What's Needed for Phase 1B

Phase 1B will focus on **Infrastructure Layer Implementation** - bringing the domain and application layers to life with:

### 1. **Entity Framework Core Integration** (3-4 days)
```
Create Files:
- src/AiUberMarketplace.Infrastructure/DbContext.cs
- src/AiUberMarketplace.Infrastructure/EntityConfigurations/*.cs
- src/AiUberMarketplace.Infrastructure/Repositories/*.cs

Tasks:
□ Create ApplicationDbContext extending DbContext
□ Configure all entities with Fluent API
□ Setup event store table for outbox pattern
□ Configure optimistic locking (Timestamp/RowVersion)
□ Create PostgreSQL migrations
□ Test all CRUD operations

Technologies:
- Entity Framework Core 10.0
- PostgreSQL (using Npgsql provider)
- Pomelo for multi-database support (optional)
```

### 2. **Dependency Injection & Middleware** (2-3 days)
```
Create Files:
- src/AiUberMarketplace.API/Program.cs
- src/AiUberMarketplace.API/ServiceExtensions.cs
- src/AiUberMarketplace.API/ExceptionHandlingMiddleware.cs

Tasks:
□ Setup Dependency Injection container
  - Register DbContext
  - Register repositories
  - Register application services
  - Register AutoMapper profiles
□ Configure HTTP pipeline
□ Add exception handling middleware with RFC 7807 Problem Details
□ Add logging (Serilog)
□ Add OpenTelemetry tracing
□ Add health checks

Technologies:
- Microsoft.Extensions.DependencyInjection
- Serilog
- OpenTelemetry
- MediatR (optional for CQRS)
```

### 3. **API Controllers** (3-4 days)
```
Create Files:
- src/AiUberMarketplace.API/Controllers/RidersController.cs
- src/AiUberMarketplace.API/Controllers/DriversController.cs
- src/AiUberMarketplace.API/Controllers/RidesController.cs
- src/AiUberMarketplace.API/Controllers/TripsController.cs

Endpoints:
POST   /api/v1/riders                     Create rider
GET    /api/v1/riders/{riderId}           Get rider
POST   /api/v1/drivers                    Create driver
POST   /api/v1/drivers/{driverId}/online  Go online
POST   /api/v1/drivers/{driverId}/offline Go offline
GET    /api/v1/drivers/{driverId}         Get driver
POST   /api/v1/rides                      Request ride
GET    /api/v1/rides/{rideId}             Get ride
POST   /api/v1/rides/{rideId}/accept      Accept offer (with idempotency)
GET    /api/v1/trips/{tripId}             Get trip
POST   /api/v1/trips/{tripId}/start       Start trip
POST   /api/v1/trips/{tripId}/complete    Complete trip

Technologies:
- ASP.NET Core 10.0 Minimal APIs
- OpenAPI/Swagger documentation
- Input validation (FluentValidation)
- Request/Response mapping (AutoMapper)
```

### 4. **Outbox Pattern Implementation** (2-3 days)
```
Create Files:
- src/AiUberMarketplace.Infrastructure/Outbox/OutboxService.cs
- src/AiUberMarketplace.Infrastructure/BackgroundJobs/PublishOutboxEventsJob.cs

Tasks:
□ Create OutboxMessage table in PostgreSQL
□ Implement transactional outbox writes
  - Domain events stored in outbox within same transaction as aggregate
  - No split transactions = no lost events
□ Create background service to publish outbox to Kafka/Redpanda
  - Poll OutboxMessage table
  - Publish to Kafka
  - Mark as published
  - Retry on failure
□ Add idempotency tracking
□ Add distributed tracing (correlation IDs)

Technologies:
- Kafka/Redpanda (already in docker-compose)
- System.Threading.Timer or BackgroundService
- Polly for retry policies
- Jaeger for tracing (already in docker-compose)
```

### 5. **Integration Tests** (2-3 days)
```
Create Files:
- tests/AiUberMarketplace.Integration/Fixtures/IntegrationTestFixture.cs
- tests/AiUberMarketplace.Integration/RiderControllerTests.cs
- tests/AiUberMarketplace.Integration/DriverControllerTests.cs
- tests/AiUberMarketplace.Integration/RideControllerTests.cs
- tests/AiUberMarketplace.Integration/ConcurrencyTests.cs

Tasks:
□ Create test database fixture (PostgreSQL in Docker)
□ Test all API endpoints with real database
□ Test event publishing to Kafka
□ Test concurrent operations (double-assignment scenarios)
□ Test idempotency (same request returns same result)
□ Test error cases with RFC 7807 responses

Technologies:
- xUnit
- TestContainers for PostgreSQL
- Moq for external dependencies
- WebApplicationFactory for API testing
```

## Implementation Order for Phase 1B

### Week 1
1. **Day 1-2**: EF Core DbContext + Repository implementations
   - Configure all entities
   - Setup migrations
   - Test CRUD operations

2. **Day 3**: Dependency Injection setup
   - Wire all services
   - Configure middleware

### Week 2
3. **Day 4-5**: API Controllers
   - Implement REST endpoints
   - Add validation
   - Add documentation

4. **Day 6-7**: Outbox Pattern
   - Event publishing to Kafka
   - Background job for async publishing

### Week 3
5. **Day 8-9**: Integration Tests
   - Full flow testing with real database
   - Concurrency testing

6. **Day 10**: Documentation & Finalization
   - API documentation
   - Deployment guide

---

## File Structure After Phase 1B

```
ai-uber-marketplace/
├── src/
│   ├── AiUberMarketplace.Domain/           ✅ Done
│   │   ├── Entities.cs
│   │   └── ...
│   │
│   ├── AiUberMarketplace.Application/      ✅ Done
│   │   ├── ApplicationServices.cs
│   │   └── ...
│   │
│   ├── AiUberMarketplace.Infrastructure/   🔄 Phase 1B
│   │   ├── DbContext.cs
│   │   ├── EntityConfigurations/
│   │   ├── Repositories/
│   │   ├── Outbox/
│   │   └── BackgroundJobs/
│   │
│   └── AiUberMarketplace.API/              🔄 Phase 1B
│       ├── Program.cs
│       ├── Controllers/
│       ├── Middleware/
│       ├── ServiceExtensions.cs
│       └── appsettings.json
│
├── tests/
│   ├── AiUberMarketplace.Tests/            ✅ Done (Unit Tests)
│   │
│   └── AiUberMarketplace.Integration/      🔄 Phase 1B
│       ├── Fixtures/
│       └── *ControllerTests.cs
│
├── docker-compose.yml                      ✅ Ready (PostgreSQL, Redis, Kafka, etc)
├── AiUberMarketplace.sln
└── ...
```

---

## Testing Strategy for Phase 1B

### Unit Tests (Already Implemented) ✅
- Domain logic (54 tests)
- Service operations
- No database dependency

### Integration Tests (Phase 1B) 🔄
- End-to-end API flows
- Database operations
- Event publishing
- Concurrency scenarios
- Real infrastructure components

### Performance Tests (Phase 2+)
- Latency benchmarks (target: <200ms for ride requests)
- Throughput tests (target: 231 RPS)
- GPS update handling (target: 750K/sec)

---

## Key Considerations for Phase 1B

### 1. Database Schema
```sql
-- Event Store (for outbox pattern)
CREATE TABLE event_store.outbox_messages (
    id UUID PRIMARY KEY,
    aggregate_id UUID NOT NULL,
    aggregate_type VARCHAR(255) NOT NULL,
    event_type VARCHAR(255) NOT NULL,
    event_payload JSONB NOT NULL,
    created_at TIMESTAMP NOT NULL,
    published_at TIMESTAMP,
    version INT NOT NULL
);

-- All other tables configured via EF Core Fluent API
```

### 2. Idempotency Key Handling
```csharp
// Cache idempotency keys for 24 hours
// If duplicate request detected, return cached response
// Prevents double-charging, duplicate trip creation
```

### 3. Optimistic Locking Implementation
```csharp
// EF Core's [Timestamp] attribute on RowVersion byte[]
// Automatically increments on each update
// DbUpdateConcurrencyException if version mismatch
```

### 4. Connection Pooling
```
// PostgreSQL: Min 10, Max 100 connections
// Adjust based on expected concurrency (500M users, 231 RPS)
```

### 5. Monitoring & Observability
```
- Jaeger tracing for distributed requests
- Prometheus metrics for all operations
- Health checks for database, Kafka, Redis
- Correlation IDs across all logs
```

---

## Commands for Phase 1B

```bash
# Create Infrastructure project
dotnet new classlib -n AiUberMarketplace.Infrastructure -o src/AiUberMarketplace.Infrastructure
dotnet new classlib -n AiUberMarketplace.API -o src/AiUberMarketplace.API
dotnet new xunit -n AiUberMarketplace.Integration -o tests/AiUberMarketplace.Integration

# Add NuGet packages
dotnet add src/AiUberMarketplace.Infrastructure package EntityFrameworkCore
dotnet add src/AiUberMarketplace.Infrastructure package Npgsql.EntityFrameworkCore.PostgreSQL
dotnet add src/AiUberMarketplace.API package AspNetCore.OpenApi

# Build and test
dotnet build
dotnet test
```

---

## Success Criteria for Phase 1B

- [ ] All CRUD operations working with PostgreSQL
- [ ] 10+ integration tests passing
- [ ] All API endpoints working
- [ ] Events publishing to Kafka
- [ ] Idempotency working (same request → same response)
- [ ] Concurrency tests passing (optimistic locking)
- [ ] OpenAPI documentation generated
- [ ] Health checks responding
- [ ] Distributed tracing working
- [ ] Error handling with RFC 7807

---

## References

- **EF Core Documentation**: https://learn.microsoft.com/en-us/ef/core/
- **ASP.NET Core Minimal APIs**: https://learn.microsoft.com/en-us/aspnet/core/fundamentals/minimal-apis
- **Outbox Pattern**: https://microservices.io/patterns/data/transactional-outbox.html
- **RFC 7807 Problem Details**: https://tools.ietf.org/html/rfc7807
- **Optimistic Concurrency**: https://learn.microsoft.com/en-us/ef/core/saving/concurrency
- **Idempotency Key Pattern**: https://stripe.com/blog/idempotency
- **Domain-Driven Design**: https://martinfowler.com/bliki/DomainDrivenDesign.html

---

**Estimated Duration for Phase 1B**: 10-12 days  
**Target Completion**: End of January 2025  
**Status**: Ready to begin implementation 🚀
