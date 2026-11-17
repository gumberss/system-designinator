# ADR-0001: Backend Language Selection

**Status**: ACCEPTED

**Date**: 2026-11-11

**Participants**: Architecture Team

## Context

We are building a distributed ride-hailing platform that must handle:
- Millions of concurrent WebSocket connections
- Hundreds of thousands of events per second
- Complex async workflows (location updates, ride matching, payments)
- Real-time geospatial queries
- Strong consistency requirements for critical transactions

The backend language choice directly impacts:
- Developer productivity and code maintainability
- System performance under load
- Operational complexity (observability, debugging)
- Team expertise and hiring
- Cost of running at scale

## Decision

**We choose C# with .NET 9 as the backend language.**

## Rationale

### Async/Await Model
C# has the most mature async/await implementation:
- Structured concurrency with task-based parallelism
- Efficient connection pooling and thread management
- Native support for distributed tracing and correlation IDs
- No callback hell or promise chains

This is essential for handling millions of concurrent WebSocket connections.

### Strong Type System
- Compile-time null safety (nullable reference types)
- Discriminated unions and pattern matching (C# 8+)
- Excellent IDE support and refactoring tools
- Catches bugs before runtime

Critical for reducing race conditions and state machine bugs.

### Built-in Distributed Systems Support
- SignalR for WebSocket communication with automatic reconnection
- Entity Framework Core with excellent transaction support
- Middleware pipeline for cross-cutting concerns
- Native HTTP/2 and gRPC support

### Ecosystem Quality
- ASP.NET Core is production-grade and used at massive scale
- Outstanding observability: OpenTelemetry has first-class support
- Strong community libraries for common patterns (polly for retries, mediatr for CQRS)
- Excellent unit testing frameworks (xUnit, Moq, Shouldly)

### Performance Characteristics
- JIT compilation provides excellent runtime performance
- Efficient memory management with modern GC algorithms
- Native compilation support (NativeAOT) for edge deployment
- Comparable to Java and Go in benchmark tests

### Operational Simplicity
- Single deployable binary (no external runtime required with .NET)
- Excellent debugging support
- Strong APM integration (Application Insights, New Relic, Datadog)
- Good fit for container orchestration

## Alternatives Considered

### 1. Go
**Pros**: Lightweight goroutines, fast compilation, built for concurrency
**Cons**: Weak error handling (nil checks), no built-in async framework, less mature ecosystem for domain modeling
**Verdict**: Excellent for systems programming, but less suitable for business logic complexity

### 2. Python (FastAPI/asyncio)
**Pros**: Rapid development, great for data processing, easy to learn
**Cons**: GIL limits true parallelism, harder to scale, operational complexity, type safety issues
**Verdict**: Great for prototyping, but production reliability concerns at this scale

### 3. Java (Spring Boot)
**Pros**: Massive ecosystem, well-tested at scale, strong type system
**Cons**: Heavier resource usage, slower startup, more complex observability setup
**Verdict**: Equally viable but .NET has simpler async model

### 4. Node.js (TypeScript + Express/Fastify)
**Pros**: Single language full-stack, event-driven architecture native
**Cons**: Single-threaded, CPU-bound tasks difficult, library quality inconsistency
**Verdict**: Better for frontend; backend async is more awkward than C#

### 5. Rust
**Pros**: Blazing fast, memory safe, excellent error handling
**Cons**: Steep learning curve, slower development velocity, smaller ecosystem
**Verdict**: Over-engineered for business logic; better for infrastructure components

## Trade-offs

### Advantages of C#/.NET
✅ Async/await is the gold standard  
✅ Strong type safety reduces bugs  
✅ Excellent real-time support (SignalR)  
✅ Outstanding observability integration  
✅ Developer productivity (refactoring, IDE support)  
✅ Cost-effective at scale  

### Disadvantages of C#/.NET
❌ Smaller hiring pool than Java or Go  
❌ Steeper learning curve than Python  
❌ Windows-centric historically (mitigated by .NET Core)  
❌ Less embedded in systems programming (not relevant here)  

## Consequences

### Development
- All services written in C# with .NET 9
- Use ASP.NET Core for REST APIs
- Use SignalR for WebSocket real-time communication
- Use Entity Framework Core for ORM and transactions

### Deployment
- Single binary per service (no external runtime)
- Container images are relatively small (~200MB base)
- Good compatibility with Kubernetes and cloud platforms

### Team
- Requires C# developers (or willing to learn)
- Strong IDE support reduces onboarding friction
- Plenty of .NET community resources

### Performance
- Fast JIT startup (~100-500ms cold start)
- Efficient async handling for millions of connections
- Predictable GC pauses with modern algorithms

### Operations
- Native support for health checks and liveness probes
- Built-in metrics and tracing
- Easy integration with APM tools
- Debugging tools are excellent (Visual Studio, Visual Studio Code)

## Related Decisions

- [ADR-0002: Database Architecture](0002-database-architecture.md) - PostgreSQL chosen for consistency with .NET ORM
- [ADR-0005: Real-Time Communication](0005-websocket-real-time.md) - SignalR chosen for WebSocket layer
- [ADR-0009: Observability Strategy](0009-observability-strategy.md) - OpenTelemetry integration

## References

- [.NET Performance Benchmarks](https://benchmarksgame.alioth.debian.org/)
- [ASP.NET Core Architecture Guide](https://learn.microsoft.com/en-us/aspnet/core/)
- [SignalR Documentation](https://learn.microsoft.com/en-us/aspnet/core/signalr)
- [Entity Framework Core Docs](https://learn.microsoft.com/en-us/ef/core/)
- [Distributed Systems in .NET](https://particular.net/blog)

## Implementation Checklist

- [ ] Create .NET 9 project structure
- [ ] Set up ASP.NET Core WebAPI template
- [ ] Configure Entity Framework Core
- [ ] Add OpenTelemetry instrumentation
- [ ] Set up structured logging
- [ ] Create CI/CD pipeline for .NET builds
- [ ] Document coding standards
- [ ] Establish development environment setup guide
