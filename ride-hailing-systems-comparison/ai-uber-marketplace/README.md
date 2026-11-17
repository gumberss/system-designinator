# AI Uber Marketplace - Distributed Systems Learning Laboratory

A production-oriented ride-hailing platform designed as an advanced distributed systems learning laboratory.

This project is **not** just about building an application that works. It's about understanding how real-world ride-hailing platforms work internally, how architectural decisions affect scalability, and how systems behave under concurrency, failures, network partitions, and massive workloads.

## Project Vision

Build a complete ride-hailing marketplace connecting riders and drivers with support for:

- Rider and driver registration/authentication
- Real-time driver location tracking
- Nearby driver discovery (geospatial search)
- Dynamic ride matching with concurrency safety
- Real-time trip tracking via WebSockets
- ETA calculation and prediction
- Payment processing with fraud detection
- AI-powered rider and operations assistance
- Complete observability and failure simulation

## Scale Assumptions

Designed for:
- 500 million registered riders
- 20 million daily active riders
- 5 million registered drivers
- 3 million daily active drivers
- 20 million daily trips
- Driver location updates every 4 seconds
- Millions of concurrent WebSocket connections

Detailed throughput calculations and scaling strategies in [docs/01-architecture.md](docs/01-architecture.md).

## Implementation Phases

### Phase 0: Architecture Foundation ✏️ (Current)
- Requirements and scope
- C4 architecture diagrams
- Bounded contexts and domain model
- Technology stack selection
- Architecture Decision Records (ADRs)
- Data ownership map
- Event catalog
- API contracts
- Local development environment

### Phase 1: Core Ride Lifecycle
- Rider/driver registration
- Driver availability
- Ride requests and matching
- Trip state machine
- Ride cancellation and completion

### Phase 2: Real-Time Location & Geospatial Search
- Driver GPS updates (4-second intervals)
- Redis GEO vs Quadtree comparison
- Nearby driver discovery
- Historical location persistence
- Real-time map visualization

### Phase 3: Event-Driven Architecture
- Kafka/Redpanda integration
- Transactional Outbox pattern
- Idempotent consumers
- Dead-letter queues
- Event replay and versioning

### Phase 4: Real-Time Communication
- WebSocket infrastructure
- Real-time trip tracking
- Driver offers and notifications
- Connection management and reconnection

### Phase 5: ETA and Routing Engine
- Road network graph
- Route calculation (A*/Dijkstra)
- Traffic-adjusted weights
- ETA prediction and correction
- ML prediction interface

### Phase 6: Payments and Fraud Detection
- Payment authorization/capture
- Fraud scoring and investigation
- Payment reconciliation
- State machine for payment lifecycle

### Phase 7: Reliability and Fault Tolerance
- Circuit breakers and bulkheads
- Failure injection across architecture
- Database replication
- SLOs and error budgets
- Graceful degradation

### Phase 8: Architecture Simulator
- Workload configuration interface
- Failure injection controls
- Geographic visualization
- Real-time metrics dashboard
- Experiment recording and comparison

### Phase 9: AI Integration
- Rider AI Assistant (natural language requests)
- Operations AI Agent (failure analysis)
- Fraud Analyst (behavioral investigation)
- Architecture Assistant (RAG over documentation)

### Phase 10: Scale and Production Readiness
- Load testing and optimization
- Geographic sharding
- Database partitioning
- Multi-region architecture
- Disaster recovery

## Technology Stack (Phase 0 Proposal)

| Component | Technology | Justification |
|-----------|-----------|---------------|
| **Backend** | .NET 9 (C#) | Strong async/await, type safety, excellent distributed systems support |
| **Database** | PostgreSQL (primary) | ACID transactions, geospatial extensions (PostGIS), proven at scale |
| **Cache** | Redis | Geo commands, low-latency location ingestion, familiar ecosystem |
| **Message Queue** | Redpanda | Kafka-compatible, simpler deployment, excellent performance |
| **Real-Time** | WebSockets (.NET SignalR) | Native support, automatic reconnection, proven for millions of connections |
| **Routing Engine** | Custom implementation | Full control over routing logic and traffic modeling |
| **Frontend** | React + TypeScript | Real-time updates, map visualization |
| **Observability** | OpenTelemetry + Jaeger/Prometheus | Vendor-neutral, structured logging, distributed tracing |
| **Container Runtime** | Docker + Docker Compose | Local development parity with production |
| **Infrastructure** | Bicep (later phases) | Azure Infrastructure as Code |

## Repository Structure

```
ai-uber-marketplace/
├── docs/                          # Architecture documentation
│   ├── 01-architecture.md        # System design overview
│   ├── 02-bounded-contexts.md    # Domain model and service boundaries
│   ├── 03-technology-stack.md    # Technology decisions and trade-offs
│   ├── 04-event-catalog.md       # All domain events
│   ├── 05-api-contracts.md       # Service interfaces
│   ├── adrs/                     # Architecture Decision Records
│   │   ├── 0001-backend-language.md
│   │   ├── 0002-database-choice.md
│   │   ├── 0003-geospatial-indexing.md
│   │   └── ...
│   └── diagrams/                 # C4 architecture diagrams
├── src/                          # Backend implementation
│   ├── AiUberMarketplace.Api/
│   ├── AiUberMarketplace.Application/
│   ├── AiUberMarketplace.Domain/
│   ├── AiUberMarketplace.Infrastructure/
│   └── AiUberMarketplace.Web/    # Real-time services and SignalR
├── frontend/                      # React + TypeScript
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   └── services/
│   └── package.json
├── infra/                        # Infrastructure as Code
│   ├── docker-compose.yml
│   └── bicep/                    # Bicep templates (Phase 10)
├── tests/                        # Test suite
│   ├── AiUberMarketplace.Tests/
│   ├── AiUberMarketplace.Integration.Tests/
│   └── AiUberMarketplace.Contract.Tests/
├── scripts/                      # Development utilities
│   ├── setup.sh
│   ├── seed-data.sh
│   └── load-test.sh
└── .editorconfig
```

## Getting Started

### Prerequisites
- .NET 9 SDK
- Docker and Docker Compose
- Node.js 18+
- PostgreSQL (local or via Docker)
- Redis (local or via Docker)

### Local Development Setup

```bash
# Clone and navigate to project
cd ai-uber-marketplace

# Start infrastructure (PostgreSQL, Redis, Redpanda)
docker-compose up -d

# Build backend
dotnet build

# Run migrations
dotnet ef database update

# Start backend
dotnet run --project src/AiUberMarketplace.Api

# In another terminal: setup and start frontend
cd frontend
npm install
npm run dev
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for detailed setup instructions.

## Core Concepts

### Bounded Contexts
The system is organized into logical domains:
1. **Identity** - User registration, authentication, authorization
2. **Rider** - Rider profiles, preferences, history
3. **Driver** - Driver profiles, vehicle management, availability
4. **Location** - Real-time driver location ingestion
5. **Geospatial** - Nearby driver discovery
6. **Matching** - Matching riders to drivers
7. **Trip** - Trip lifecycle and state management
8. **ETA** - Route calculation and arrival time prediction
9. **Payment** - Payment processing and reconciliation
10. **Fraud** - Fraud detection and investigation
11. **Notification** - Real-time notifications
12. **Rating** - Reviews and ratings
13. **AI** - AI assistants and agents
14. **Analytics** - System metrics and business intelligence

### Event-Driven Communication
Services communicate through domain events:
- `RideRequested` → Matching Service
- `DriverLocationUpdated` → Location/Geospatial Services
- `DriverOfferSent` → Rider notifications
- `RideAccepted` → Trip Management Service
- `TripStarted` → Payment Service
- `TripCompleted` → Payment/Rating Services
- `PaymentCaptured` → Analytics

### Consistency Guarantees
- **Transactional**: Ride matching, payment operations
- **Eventual**: Analytics, notifications, rating updates

## Acceptance Criteria for Phase 0

- [ ] C4 diagrams created and reviewed
- [ ] All bounded contexts defined with responsibility matrix
- [ ] Technology stack documented with trade-off analysis
- [ ] At least 5 ADRs written (backend, database, geospatial, etc.)
- [ ] Event catalog defined for all critical flows
- [ ] API contracts specified for Phase 1 (Swagger/OpenAPI)
- [ ] Repository structure implemented
- [ ] Docker Compose infrastructure ready
- [ ] Development setup documented and tested
- [ ] Architecture explanation document written

## Documentation

- [Architecture Overview](docs/01-architecture.md)
- [Bounded Contexts & Domain Model](docs/02-bounded-contexts.md)
- [Technology Stack & Trade-offs](docs/03-technology-stack.md)
- [Event Catalog](docs/04-event-catalog.md)
- [API Contracts](docs/05-api-contracts.md)
- [Architecture Decision Records](docs/adrs/README.md)

## Team Expectations

This is a **mentorship project**. Every phase includes:
1. **Explain** - Describe objectives, concepts, and trade-offs
2. **Design** - Create diagrams, contracts, and decision records
3. **Implement** - Write production-oriented code
4. **Test** - Verify critical scenarios
5. **Observe** - Expose metrics, logs, and traces
6. **Demonstrate** - Show functionality locally
7. **Review** - Discuss learnings and technical debt
8. **Gate** - Verify all criteria met before next phase

No phase is skipped. No acceptance criterion is silently omitted.

## License

MIT
