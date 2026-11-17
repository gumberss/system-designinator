# ADR-0003: Geospatial Indexing Strategy

**Status**: ACCEPTED

**Date**: 2026-11-11

**Participants**: Architecture Team

## Context

The system must answer: **"Which drivers are closest to this rider?"** potentially millions of times per day.

Critical requirements:
- Latency: <100ms for geospatial queries
- Throughput: Handle location updates from 3M active drivers every 4 seconds
- Accuracy: Query radius from 100m to 50km
- Scalability: Coverage from single city to 100+ cities

Geospatial indexing strategies differ significantly:

1. **Redis GEO** - Simple, proven, but slower for large result sets
2. **Quadtree** - Fast, complex implementation, excellent for hot spots
3. **Geohash** - Good balance, used by Uber's H3 library
4. **PostGIS** - Feature-rich, slower than Redis/Quadtree, easier to query

## Decision

**Implement both Redis GEO and Quadtree, with Redis GEO as Phase 2 MVP and Quadtree as comparison.**

### Phase 2 Initial (Redis GEO)
- Store all active driver locations in Redis sorted sets
- Use GEORADIUS to find drivers within radius
- Update locations every 4 seconds
- Sufficient for MVP with <1M active drivers

### Phase 5 Evaluation (Quadtree)
- Implement parallel Quadtree structure
- Run A/B tests comparing latency/throughput
- Document performance characteristics
- Decision on production strategy at Phase 10

## Rationale

### Why Start with Redis GEO?

**Simplicity**
```csharp
// Redis GEO API is straightforward
await redis.GeoAddAsync(key: "drivers", members);
var nearby = await redis.GeoRadiusAsync(key, latitude, longitude, radius);
```

**Predictable Performance**
- O(N+log(M)) where N = returned items, M = total items
- Query radius 1km: ~10-50 drivers (fast)
- Query radius 5km: ~100-500 drivers (still <50ms)
- Query radius 50km: ~5,000-50,000 drivers (may exceed 100ms)

**Proven at Scale**
- Uber, Google Maps, Lyft all use Redis GEO for initial layers
- Simple to debug and monitor
- No complex data structures to maintain

**Eventual Consistency Acceptable**
- Location updates are already eventual (every 4 seconds)
- Delivering 30-second-old location data is acceptable
- Driver acceptance/rejection is instant (real transaction)

### Why Evaluate Quadtree?

**Better Asymptotic Performance**
- O(log N + K) where K = returned items
- Scales better to millions of items
- Constant-time radius refinement

**Naturally Partitionable**
- Each grid cell can be queried independently
- Geographic sharding aligns with quadtree structure
- Multi-city deployments benefit from tree structure

**Research Value**
- Opportunity to understand optimization trade-offs
- Document when to switch from simple to complex solutions
- Build transferable knowledge for team

## Alternatives Considered

### 1. PostGIS Only
**Pros**: Feature-rich, supports complex queries, ACID guarantees
**Cons**: Slower than Redis (10x latency for radius queries), cannot handle 750K updates/sec
**Verdict**: Revisit as fallback if Redis becomes bottleneck

### 2. Geohash Only
**Pros**: Good balance, easier to implement than Quadtree, partition-friendly
**Cons**: Loss of precision at edges, fixed precision levels, manual range queries
**Verdict**: Alternative to Quadtree if complexity becomes issue

### 3. S2 Geometry (Google's H3)
**Pros**: Mathematically elegant, hierarchical partitioning, proven at scale
**Cons**: Significant implementation complexity, steep learning curve, library maturity
**Verdict**: Defer to Phase 10 if geographic sharding becomes critical

### 4. Elasticsearch GEO
**Pros**: Full-text search + geo, complex query support
**Cons**: Overkill for simple radius queries, additional operational burden
**Verdict**: Rejected for matching service; useful for search Phase 10

### 5. Cassandra Geospatial
**Pros**: Scales with database, consistent with historical storage
**Cons**: Slower than Redis, significant query complexity, operational overhead
**Verdict**: Only if polyglot store already includes Cassandra

## Trade-offs

### Redis GEO Advantages
✅ Simple to implement and understand  
✅ Fast for small result sets (<1000 drivers)  
✅ Natural fit with location update workflow  
✅ Built-in TTL for stale location expiration  
✅ Pub/sub for real-time updates  
✅ Low operational complexity  

### Redis GEO Disadvantages
❌ Slower for large radius queries (>5km in dense areas)  
❌ Cannot express complex geographic constraints  
❌ No multi-dimensional range queries  
❌ Requires full dataset in memory  

### Quadtree Advantages
✅ Excellent performance at all scales (O(log N))  
✅ Natural geographic partitioning  
✅ Scales to arbitrary dataset size  
✅ Supports complex geo-temporal queries  
✅ Works with disk-based storage  

### Quadtree Disadvantages
❌ Significant implementation complexity  
❌ Debugging spatial algorithms is hard  
❌ Requires careful rebalancing  
❌ Build/rebuild costs  

## Consequences

### Phase 2 (Initial Implementation)
```
Driver Location Update (every 4 seconds)
  → Redis GEOADD to "drivers:active" sorted set
  → Location data persists for 30 seconds (TTL)
  → When rider requests ride:
    → Redis GEORADIUS returns drivers within 5km
    → Matching service ranks and offers to drivers
```

### Phase 5 (Evaluation)
```
Parallel Implementation:
- Redis GEO continues
- Quadtree built from same location stream
- Latency/accuracy metrics collected for both
- A/B test with 10% traffic to Quadtree
- Document performance findings
```

### Phase 10 (Production)
```
Decision point:
1. If Redis GEO sufficient (avg radius <5km, <100K drivers/city):
   - Continue Redis GEO
   - Add read replicas for HA
   
2. If Quadtree needed (avg radius >5km, >100K drivers/city):
   - Implement Quadtree cluster
   - Geographic sharding per city
   - Redis GEO as secondary index
```

### Data Model

```csharp
// Redis GEO storage
Redis ZADD drivers:active {longitude} {latitude} {driverId}

// Optional: auxiliary indexes in Redis
Redis HSET drivers:location:{driverId} lat longitude lat latitude

// Quadtree structure (if implemented)
class QuadtreeNode {
    Bounds bounds;
    List<Driver> drivers;  // if leaf
    QuadtreeNode[] children;  // if internal
    long lastUpdated;
}
```

### Monitoring & Observability

```
Metrics to track:
- drivers:geo:query_latency_p95
- drivers:geo:query_latency_p99
- drivers:geo:results_returned (histogram)
- drivers:geo:radius_requested (histogram)
- drivers:location:update_latency_p99
- drivers:location:stale_count (>10 seconds old)
```

## Related Decisions

- [ADR-0002: Database Architecture](0002-database-architecture.md) - Redis chosen for location storage
- [ADR-0006: Consistency Model](0006-consistency-model.md) - Eventual consistency acceptable for location
- [Phase 2: Real-Time Location](../01-architecture.md#phase-2-real-time-location--geospatial-search) - Detailed Phase 2 requirements

## Implementation Checklist (Phase 2)

- [ ] Redis GEO wrapper class (StackExchange.Redis)
- [ ] Driver location update consumer (from Kafka)
- [ ] Redis GEOADD batch operations
- [ ] Redis GEORADIUS query wrapper
- [ ] Stale location detection job
- [ ] Location query latency instrumentation
- [ ] Unit tests for edge cases (wrapping, radius calculation)
- [ ] Integration tests with Kafka/Redis
- [ ] Benchmark tests (1K, 10K, 100K drivers)
- [ ] Documentation of geo query semantics

## Implementation Checklist (Phase 5)

- [ ] Quadtree implementation (with tests)
- [ ] Parallel location stream to both Redis and Quadtree
- [ ] A/B testing framework for geo queries
- [ ] Latency comparison dashboard
- [ ] Accuracy validation tests
- [ ] Decision documentation

## References

- [Redis Geospatial Index](https://redis.io/commands/geoadd/)
- [Redis GEO Performance](https://redis.io/docs/data-types/geospatial/)
- [Uber's H3 Hierarchical Geospatial Index](https://h3geo.org/)
- [Quadtree Overview](https://en.wikipedia.org/wiki/Quadtree)
- [Geospatial Indexing Comparison](https://www.uber.com/en-US/blog/h3/)
