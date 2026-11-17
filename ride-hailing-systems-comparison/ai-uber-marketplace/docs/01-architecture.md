# 01 - Architecture Overview

## System Architecture Diagram (C4 Context)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        External Systems                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐               │
│  │ Maps Service │  │ Payment GW   │  │ SMS Gateway  │               │
│  └──────────────┘  └──────────────┘  └──────────────┘               │
└────────┬───────────────────┬───────────────────┬────────────────────┘
         │                   │                   │
┌────────▼───────────────────▼───────────────────▼────────────────────┐
│                      AI Uber Marketplace System                      │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                        Web/Mobile Frontend                   │   │
│  │  (React + TypeScript, Real-time Map, Trip Management)       │   │
│  └───────────────┬───────────────────────────────────────────┬─┘   │
│                  │                                           │      │
│  ┌───────────────▼───────────────────────────────────────────▼──┐  │
│  │               API Gateway & Real-Time Communication          │  │
│  │  (REST APIs, WebSocket Server, SignalR Hub, Request Router) │  │
│  └───────────────┬───────────────────────────────────────────┬──┘  │
│                  │                                           │      │
│  ┌───────────────▼──────────────────────────────────────────────┐  │
│  │                    Microservices Layer                       │  │
│  │                                                              │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │  │
│  │  │ Identity │  │  Rider   │  │ Driver   │  │ Location │   │  │
│  │  │ Service  │  │ Service  │  │ Service  │  │ Service  │   │  │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │  │
│  │       │             │             │             │         │  │
│  │  ┌────▼──────┐  ┌────▼──────┐  ┌──▼────────┐  ┌─▼──────┐ │  │
│  │  │ Geospatial│  │  Matching │  │   Trip    │  │Payment │ │  │
│  │  │  Service  │  │  Service  │  │ Management│  │Service │ │  │
│  │  └────┬──────┘  └────┬──────┘  └──┬───────┘  └─┬──────┘ │  │
│  │       │             │             │            │        │  │
│  │  ┌────▼──────┐  ┌────▼──────┐  ┌──▼────────┐  ┌─▼──────┐ │  │
│  │  │    ETA    │  │   Fraud   │  │Notification│ │Rating  │ │  │
│  │  │  Service  │  │ Detection │  │  Service   │ │Service │ │  │
│  │  └──────────┘  └──────────┘  └────────────┘  └────────┘ │  │
│  │                                                              │  │
│  └──────────┬────────────────────────────────────────────┬─────┘  │
│             │                                            │         │
│  ┌──────────▼─────────────────────────────────────────┬─▼──────┐  │
│  │            Event-Driven Backbone                    │        │  │
│  │  (Kafka/Redpanda, Topic Subscriptions, Consumers)  │        │  │
│  │                                                     │        │  │
│  │  Topics:                                            │        │  │
│  │  - ride-requested, driver-location-updated         │        │  │
│  │  - driver-offered, ride-accepted, trip-started     │        │  │
│  │  - payment-authorized, fraud-detected              │        │  │
│  │                                                     │        │  │
│  └─────────────────────────────────────────────────────┘        │  │
│                                                                   │  │
│  ┌──────────────────────────────────────────────────────────┐   │  │
│  │           Data & Caching Layer                           │   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │   │  │
│  │  │ PostgreSQL   │  │    Redis     │  │  Cassandra   │   │   │  │
│  │  │ (Transactional)  (Location &   │  │  (Historical)   │   │  │
│  │  │              │  │  Matching)   │  │              │   │   │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘   │   │  │
│  └──────────────────────────────────────────────────────────┘   │  │
│                                                                   │  │
│  ┌──────────────────────────────────────────────────────────┐   │  │
│  │          Observability & Simulation                      │   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │   │  │
│  │  │  Structured  │  │  Distributed │  │   Failure    │   │   │  │
│  │  │   Logging    │  │   Tracing    │  │  Injection   │   │   │  │
│  │  │              │  │  (Jaeger)    │  │  Simulator   │   │   │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘   │   │  │
│  └──────────────────────────────────────────────────────────┘   │  │
│                                                                   │  │
│  ┌──────────────────────────────────────────────────────────┐   │  │
│  │         AI Integration Layer (Phase 9)                   │   │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │   │  │
│  │  │Rider Assistant   │Ops Agent     │Fraud Analyst │   │   │  │
│  │  │ (Requests, ETA)  │ (Debugging)  │ (Investigation)  │   │  │
│  │  └──────────────┘  └──────────────┘  └──────────────┘   │   │  │
│  └──────────────────────────────────────────────────────────┘   │  │
│                                                                   │  │
└───────────────────────────────────────────────────────────────────┘
```

## Bounded Contexts (Domain-Driven Design)

### 1. Identity Context
**Responsibility**: User registration, authentication, authorization, session management
**Data Owner**: User credentials, roles, permissions
**Key Entities**: User, UserProfile, AuthToken, Role
**Communication**: Synchronous (internal APIs) or events on user creation

### 2. Rider Context
**Responsibility**: Rider profiles, preferences, payment methods, ride history
**Data Owner**: RiderProfile, PreferredLocations, PaymentMethods, RideHistory
**Key Entities**: Rider, RiderPreferences, SavedLocation, RideStatistics
**Events**: RiderRegistered, PreferencesUpdated

### 3. Driver Context
**Responsibility**: Driver profiles, vehicle info, license management, availability
**Data Owner**: DriverProfile, Vehicle, License, DriverStatistics, Availability
**Key Entities**: Driver, Vehicle, License, AvailabilityStatus
**Events**: DriverRegistered, AvailabilityChanged, VehicleRegistered

### 4. Location Context
**Responsibility**: Ingesting and storing driver location updates
**Data Owner**: DriverLocation, LocationSequence, LocationHistory
**Key Entities**: DriverLocation, LocationUpdate
**Events**: DriverLocationUpdated (published by drivers, consumed by geospatial service)
**Constraints**: Handle duplicates, out-of-order updates, offline drivers

### 5. Geospatial Context
**Responsibility**: Nearby driver discovery using spatial indexing
**Data Owner**: SpatialIndex (Quadtree or Redis GEO), DriverProximity
**Key Entities**: SpatialGrid, NearbyDriver, ProximityQuery
**Communication**: Queries from Matching Service
**Trade-offs**: Redis GEO (simpler, slower) vs Quadtree (complex, faster)

### 6. Matching Context
**Responsibility**: Matching riders to drivers with concurrency safety
**Data Owner**: DriverOffers, MatchingMetrics
**Key Entities**: DriverOffer, MatchingStrategy, OfferState
**Events**: DriverOfferSent, OfferExpired, OfferAccepted
**Constraints**: Prevent duplicate assignments, expired offer acceptance, race conditions

### 7. Trip Context
**Responsibility**: Trip lifecycle, state transitions, audit history
**Data Owner**: Trip, TripState, TripEvents, TripHistory
**Key Entities**: Trip, TripState, TripEvent, RouteSegment
**Events**: TripRequested, TripStarted, TripInProgress, TripCompleted, TripCancelled
**State Machine**:
```
REQUESTED → SEARCHING_DRIVER → DRIVER_ASSIGNED → DRIVER_ARRIVING 
  → DRIVER_ARRIVED → IN_PROGRESS → COMPLETED (or CANCELLED at any stage)
```

### 8. ETA Context
**Responsibility**: Route calculation, ETA prediction, traffic modeling
**Data Owner**: RoadNetwork, TrafficHistory, RoutingCache, ETAModel
**Key Entities**: Road, Segment, Route, TrafficCondition, ETAPrediction
**Communication**: Queries from Trip Service, provides ETA updates
**ML Integration**: DeepETA (corrects routing engine), DeepETT (segment-specific)

### 9. Payment Context
**Responsibility**: Payment authorization, capture, refunds, reconciliation
**Data Owner**: PaymentTransaction, PaymentState, PaymentHistory
**Key Entities**: Payment, PaymentMethod, Transaction, PaymentReconciliation
**Events**: PaymentAuthorized, PaymentCaptured, PaymentFailed, PaymentRefunded
**Constraints**: Idempotency, reconciliation with external providers

### 10. Fraud Context
**Responsibility**: Fraud scoring, detection, investigation, remediation
**Data Owner**: FraudScore, SuspiciousActivity, FraudInvestigation
**Key Entities**: FraudAlert, SuspiciousPattern, InvestigationCase
**Events**: FraudDetected, FraudInvestigationStarted, FraudCaseResolved
**Approach**: Behavioral signals (unusual locations, rapid requests), transactional signals

### 11. Notification Context
**Responsibility**: Real-time notifications to riders and drivers
**Data Owner**: Subscription, NotificationLog, NotificationPreferences
**Key Entities**: Notification, NotificationSubscription, NotificationChannel
**Communication**: WebSocket (real-time), Push (offline), Email (follow-up)

### 12. Rating Context
**Responsibility**: Ratings and reviews post-trip
**Data Owner**: Rating, Review, RatingStatistics
**Key Entities**: Rating, Review, RatingAggregate
**Events**: RatingSubmitted, ReviewPosted
**Consistency**: Eventual consistency (ratings available async)

### 13. AI Context
**Responsibility**: AI assistants, agents, and tool calling
**Data Owner**: AIAgent, AgentTrace, AIToolCall, AIDecision
**Key Entities**: RiderAssistant, OperationsAgent, FraudAnalyst, ArchitectureAssistant
**Communication**: RAG over documentation, tool calls to service APIs
**Constraints**: Never bypass transactional APIs, no direct state mutations

### 14. Analytics Context
**Responsibility**: Metrics, dashboards, business intelligence
**Data Owner**: Event aggregations, MetricSnapshots, AnalyticsCache
**Communication**: Event subscribers, eventual consistency
**Metrics**: Throughput, latency, matching efficiency, cancellation rates, revenue

## Technology Stack Decisions

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Language** | C# / .NET 9 | Async/await semantics, strong type system, excellent concurrency support |
| **Framework** | ASP.NET Core | REST APIs, SignalR (WebSockets), middleware ecosystem |
| **Database** | PostgreSQL | ACID transactions, PostGIS for geospatial, proven scalability |
| **Cache** | Redis | GEO commands, low-latency matching state, location ingestion |
| **Message Queue** | Redpanda | Kafka-compatible, simpler ops, excellent throughput |
| **Routing Engine** | Custom (Dijkstra/A*) | Full control over traffic modeling and ETA correction |
| **Frontend** | React + TypeScript | Real-time WebSocket integration, map visualization |
| **Observability** | OpenTelemetry + Jaeger | Vendor-neutral, structured tracing, works with multiple backends |
| **Infrastructure** | Docker Compose (dev), Bicep (prod) | Local parity, Azure Infrastructure as Code |

## Scale Calculations

### Throughput
- **Ride Requests/sec**: 20M trips/day ÷ 86,400 sec ≈ **231 RPS**
- **Location Updates/sec**: 3M drivers × 1 update/4s ≈ **750,000 GPS updates/sec**
- **WebSocket Connections**: 23M concurrent (20M riders + 3M drivers) = **23M concurrent**
- **Payment Events/sec**: 231 RPS (matches ride requests)

### Storage Growth (Annual)
- **Trip Data**: 20M trips/day × 365 days × ~2KB per trip ≈ **14.6 TB**
- **Location History**: 750K GPS/sec × 86,400 sec × 365 × 50 bytes ≈ **1.18 PB**
- **Transactions**: 20M/day × 365 × ~500 bytes ≈ **3.65 TB**

**Strategy**: Hot data (1 month) in PostgreSQL, warm data (1 year) in Cassandra, cold data (2+ years) in object storage.

### Database Throughput
- **PostgreSQL** (rides, drivers, payments): ~231 RPS → easily handled by single instance
- **Redis** (location, matching): 750K writes/sec → requires sharding or cluster mode
- **Cassandra** (historical): Write-optimized time-series storage

### Network Bandwidth
- **Location updates**: 750K/sec × 500 bytes ≈ **375 Mbps** (compressible to ~100 Mbps)
- **WebSocket messages**: ~500 msg/sec per user × 23M = **11.5 Tbps** (peak) → edge compression

## Consistency Model

### Strong Consistency (ACID)
1. **Ride Matching**: Transaction prevents duplicate driver assignment
2. **Payment Authorization**: Idempotent key prevents double charges
3. **Trip State Transitions**: Optimistic locking detects conflicts

### Eventual Consistency
1. **Notifications**: Async delivery after event processing
2. **Analytics**: Aggregated after 5-15 minute window
3. **Ratings**: Available async after trip completion
4. **Driver Statistics**: Updated in background job

## Failure Scenarios to Address

1. **Database Slow/Unavailable**: Circuit breaker, read replicas, caching
2. **Redis Downtime**: Fallback to database, degrade geospatial queries
3. **Kafka Broker Failure**: Replication, consumer lag tracking
4. **Network Partition**: Split-brain detection, stickiness, heartbeats
5. **Payment Provider Timeout**: Async reconciliation, retry with backoff
6. **WebSocket Disconnection**: Automatic reconnect, state synchronization
7. **Duplicate Events**: Idempotent consumer processing, deduplication cache
8. **Late State Transitions**: Event sequence validation, stale update detection

## Next Steps (Phase 0 Completion)

1. Expand each bounded context with detailed entity diagrams
2. Document all events with payload schemas
3. Create detailed API contracts (OpenAPI)
4. Write ADRs for major decisions (5-10 ADRs minimum)
5. Create entity-relationship diagrams for each data store
6. Define SLOs and monitoring strategy
7. Test local development environment with Docker Compose
8. Create acceptance criteria validation checklist
