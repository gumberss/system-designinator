# ADR-0002: Database Architecture

**Status**: ACCEPTED

**Date**: 2026-11-11

**Participants**: Architecture Team

## Context

The system must handle:
- **Transactional workloads**: Ride matching, payments, trip state (~231 RPS initially)
- **Location ingestion**: ~750,000 GPS updates per second
- **Historical data**: ~1.18 PB annually (with partitioning)
- **Geographic queries**: Nearby driver discovery for millions of concurrent riders
- **Consistency requirements**: Strong for transactions, eventual for analytics
- **Scale**: From local development to 500M+ records

Different workload patterns require different storage characteristics:
- OLTP (rides, payments) needs ACID, transactions, sub-100ms latency
- Location data needs high write throughput, eventual consistency
- Historical data needs cost-effective storage with time-based partitioning
- Geospatial queries need efficient indexing

## Decision

**Use a polyglot database architecture:**

1. **PostgreSQL** (primary transactional database)
   - Ride, driver, rider, payment, trip records
   - Strong consistency, ACID transactions
   - PostGIS for geospatial queries during early phases

2. **Redis** (caching & real-time matching)
   - Current driver locations (updates every 4 seconds)
   - Active ride offers and matching state
   - User session cache
   - Rate limiting and job queue

3. **Cassandra/ScyllaDB** (historical & time-series)
   - Location history (after warm-up period)
   - Trip history and telemetry
   - Fraud investigation data
   - Analytics events

4. **Object Storage** (cold archive)
   - Ride archives (2+ years old)
   - Historical location data (extremely compressed)
   - Backup of partition archives

## Rationale

### PostgreSQL as Primary Store

**Why PostgreSQL?**
- Industry-standard ACID compliance
- Excellent transactional support with optimistic locking
- PostGIS extension for geospatial queries
- Proven at 100,000+ RPS with proper scaling
- Strong consistency matches business requirements
- Rich query language (complex trip workflows)
- Excellent connection pooling (pgBouncer)
- Native JSON support for flexible schemas

**Trade-off: Write throughput**
- A single PostgreSQL instance handles ~1,000-5,000 writes/sec
- Location ingestion (750K/sec) must go elsewhere (Redis)
- Trip and payment operations (~230 RPS) easily fit in single instance

### Redis for Location & Matching

**Why Redis?**
- 750,000+ writes/second easily supported
- Redis GEO commands for nearby driver queries (Phase 2 to evaluate vs Quadtree)
- Automatic expiration (stale locations purged after 30 seconds)
- <1ms latency for matching operations
- Supports pub/sub for real-time notifications
- Simple operational model

**Trade-off: Consistency & Durability**
- Location data is ephemeral (4-second updates mean 30-sec-old data is stale)
- Acceptable data loss in event of total Redis failure (locations reingested in 4 seconds)
- Matching state is transient (matched rides move to PostgreSQL)

### Cassandra/ScyllaDB for Historical Data

**Why Cassandra?**
- Write-optimized time-series database
- Excellent for append-only workloads
- Efficient compression reduces storage by 10x
- Supports time-based partitioning (rolling windows)
- Geographic distribution native to architecture
- Tunable consistency (eventual consistency acceptable for analytics)

**Trade-off: Complexity**
- Additional operational burden (clustering, rebalancing)
- Eventually consistent (OK for historical analysis)
- Deferred to Phase 10 if storage becomes bottleneck

**Alternative**: PostgreSQL with TimescaleDB (simpler, good for 1-2 years of history)

### Object Storage for Cold Archive

**Why Object Storage?**
- Cost-effective: $0.01/GB/month vs $5/GB/month for hot storage
- Ideal for data accessed <1x/month
- Supports lifecycle policies (automatic tiering)
- Integrates with Azure Blob Storage or S3

**Trade-off**: Access latency (hours vs seconds) acceptable for compliance/audits

## Alternatives Considered

### 1. Single PostgreSQL for Everything
**Pros**: Simplicity, single operational model, excellent consistency
**Cons**: Cannot handle 750K/sec location ingestion, storage growth unsustainable
**Verdict**: Viable for Phase 1-2, requires partitioning and replication for Phase 10

### 2. Distributed SQL (CockroachDB, Google Cloud Spanner)
**Pros**: Distributed ACID, no polyglot complexity, scales geographically
**Cons**: Higher cost, complex transaction semantics, overkill for current scale
**Verdict**: Revisit for Phase 10 if multi-region deployment needed

### 3. MongoDB for Everything
**Pros**: Flexible schema, horizontal scaling
**Cons**: No ACID multi-document transactions (until recently), weaker consistency guarantees
**Verdict**: Rejected due to payment transaction requirements

### 4. InfluxDB for Location Data
**Pros**: Time-series optimized, great compression, native retention policies
**Cons**: Limited query capabilities, not suitable for transactional matching state
**Verdict**: Alternative for Phase 2 historical location storage instead of Cassandra

### 5. Single NoSQL Database (DynamoDB)
**Pros**: Serverless, pay-per-request, scales automatically
**Cons**: Eventual consistency only, complex transactions, expensive at 750K/sec scale
**Verdict**: Rejected for transactional workloads

## Trade-offs

### Advantages
✅ Right tool for each job (PostgreSQL ACID, Redis speed, Cassandra throughput)  
✅ Can optimize each independently (connection pools, replication, sharding)  
✅ Natural failover boundaries (Redis down ≠ rides broken)  
✅ Proven at scale (each technology used by 1000+ companies)  
✅ Strong consistency where it matters (transactions, payments)  
✅ Cost-effective (PostgreSQL licenses free, Redis efficient)  

### Disadvantages
❌ Operational complexity (3 databases to monitor)  
❌ Data consistency between systems (eventual consistency windows)  
❌ Replication and failover more complex  
❌ Developers must understand each technology  
❌ Cross-database transactions require application-level coordination  

## Consequences

### Phase 1-2 (Early Development)
- PostgreSQL + Redis only (no Cassandra complexity)
- Location history stored in PostgreSQL (partition monthly)
- Simpler operational model, easier debugging

### Phase 5+ (Scale Testing)
- Introduce Cassandra for historical location data >30 days old
- PostgreSQL focuses on hot data (<30 days)
- Implement data archival jobs

### Phase 10 (Production Scale)
- PostgreSQL replication across regions
- Redis cluster mode for HA
- Cassandra multi-region replication
- Object storage for compliance archive

### Development
- Docker Compose includes all 3 (can disable Cassandra initially)
- Different connection strings per database type
- Transaction spanning multiple databases requires saga pattern or outbox pattern

### Monitoring & Operations
- Three different monitoring strategies (per database)
- Separate backup/recovery procedures
- Cross-database consistency validation required
- Failover procedures per database type

### Cost
- PostgreSQL: ~$0.50/hour single instance → ~$350/month
- Redis: ~$0.20/hour → ~$150/month  
- Cassandra: ~$2/hour cluster → ~$1,500/month (Phase 5+)
- **Total**: ~$500/month development, ~$2,000/month production Phase 5+

## Related Decisions

- [ADR-0001: Backend Language](0001-backend-language.md) - C#/.NET requires strong ORM support
- [ADR-0003: Geospatial Indexing](0003-geospatial-indexing.md) - Location storage strategy
- [ADR-0006: Consistency Model](0006-consistency-model.md) - When to use each consistency level
- [ADR-0004: Event-Driven Communication](0004-event-driven-communication.md) - Event sourcing vs snapshots

## Implementation Checklist

- [ ] PostgreSQL Docker image setup
- [ ] Redis Docker image setup
- [ ] Create Docker Compose with all services
- [ ] Entity Framework Core models for PostgreSQL
- [ ] Redis connection pooling (StackExchange.Redis)
- [ ] Migration scripts for PostgreSQL (Flyway/Liquibase)
- [ ] PostGIS extension installed
- [ ] Backup strategy documented
- [ ] Development database seeding script
- [ ] Performance baseline tests (insert latency, query latency)

## References

- [PostgreSQL High Throughput](https://www.citusdata.com/blog/2020/02/11/postgres-high-throughput/)
- [Redis Geospatial Index](https://redis.io/commands/geoadd/)
- [Cassandra Write Throughput](https://cassandra.apache.org/_/cassandra-basics.html)
- [Distributed Transactions](https://martinfowler.com/articles/patterns-of-distributed-systems/saga.html)
