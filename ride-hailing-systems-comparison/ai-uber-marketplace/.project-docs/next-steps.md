# Próximos Passos - Phase 0 Concluída ✅

## 🎯 Situação Atual

**Phase 0: Architecture Foundation foi completada com sucesso!**

Você agora tem:
- ✅ Arquitetura documentada (C4, bounded contexts, eventos)
- ✅ 3 ADRs completos justificando decisões tecnológicas
- ✅ Infrastructure as Code pronta (Docker Compose)
- ✅ Database schema completo (PostgreSQL + PostGIS)
- ✅ Documentação abrangente (10+ arquivos .md)
- ✅ Projeto estruturado para .NET + React

---

## 📋 Checklist: Validando Phase 0

Antes de prosseguir para Phase 1, execute:

### 1. Validar Infraestrutura

```bash
# Navegar para o projeto
cd H:\Github\system-designinator\ai-uber-marketplace

# Iniciar todos os serviços
docker-compose up -d

# Aguardar ~30 segundos para services iniciarem
docker-compose ps

# Resultado esperado: todos os serviços com STATUS "healthy"
```

**Verificações rápidas**:
```bash
# PostgreSQL
psql -h localhost -U aiuber_user -d aiuber_db -c "SELECT 1;"

# Redis
redis-cli -h localhost ping
# Esperado: PONG

# Redpanda Console
curl -s http://localhost:8080 | head -20
# Esperado: HTML da console
```

### 2. Validar Documentação

Verificar se estes arquivos existem e estão bem formatados:

```bash
# Documentação Principal
cat README.md              # 9.9 KB
cat DEVELOPMENT.md         # 10.6 KB
cat PHASE0-SUMMARY.md      # 12.1 KB

# Arquitetura
cat docs/01-architecture.md                # 14 KB
cat docs/04-event-catalog.md               # 14 KB
cat docs/00-phase0-acceptance-criteria.md  # 11 KB

# ADRs
cat docs/adrs/README.md                    # 2.5 KB
cat docs/adrs/0001-backend-language.md     # 6.3 KB
cat docs/adrs/0002-database-architecture.md # 8.5 KB
cat docs/adrs/0003-geospatial-indexing.md  # 8 KB
```

### 3. Validar Estrutura de Código

```bash
# Verificar pastas criadas
ls -la src/                          # 5 projetos .NET
ls -la frontend/                     # React structure
ls -la tests/                        # Test projects
ls -la docs/adrs/                    # Architecture decisions
```

---

## 🚀 Iniciando Phase 1: Core Ride Lifecycle

Quando estiver pronto para começar Phase 1, siga estes passos:

### Passo 1: Revisar Especificações Phase 1

```bash
# Phase 1 foca na primeira "vertical slice" do sistema
# Leia a especificação em docs/00-phase0-acceptance-criteria.md
# Seção: "Phase 1: Core Ride Lifecycle"
```

**O que Phase 1 vai entregar**:
```
✅ Rider e Driver registration (com validação)
✅ Driver availability (online/offline)
✅ Ride request submission
✅ Nearby driver discovery (Redis GEO simples)
✅ Driver offer para ride
✅ Rider acceptance de offer
✅ Atomic driver assignment (sem race conditions)
✅ Trip state machine (REQUESTED → COMPLETED)
✅ Trip completion com payment
✅ 80%+ test coverage
```

### Passo 2: Criar .NET Solution

```bash
# Entrar na pasta src
cd src

# Criar solução
dotnet new sln -n AiUberMarketplace

# Criar projetos
dotnet new classlib -n AiUberMarketplace.Domain
dotnet new webapi -n AiUberMarketplace.Api
dotnet new classlib -n AiUberMarketplace.Application
dotnet new classlib -n AiUberMarketplace.Infrastructure
dotnet new classlib -n AiUberMarketplace.Web
dotnet new xunit -n AiUberMarketplace.Tests
dotnet new xunit -n AiUberMarketplace.IntegrationTests

# Adicionar projetos à solução
dotnet sln add AiUberMarketplace.Domain/AiUberMarketplace.Domain.csproj
dotnet sln add AiUberMarketplace.Api/AiUberMarketplace.Api.csproj
dotnet sln add AiUberMarketplace.Application/AiUberMarketplace.Application.csproj
dotnet sln add AiUberMarketplace.Infrastructure/AiUberMarketplace.Infrastructure.csproj
dotnet sln add AiUberMarketplace.Web/AiUberMarketplace.Web.csproj
dotnet sln add AiUberMarketplace.Tests/AiUberMarketplace.Tests.csproj
dotnet sln add AiUberMarketplace.IntegrationTests/AiUberMarketplace.IntegrationTests.csproj

# Verificar
dotnet sln list
```

### Passo 3: Instalar NuGet Packages

```bash
# No projeto Infrastructure (database)
cd AiUberMarketplace.Infrastructure
dotnet add package Microsoft.EntityFrameworkCore
dotnet add package Npgsql.EntityFrameworkCore.PostgreSQL
dotnet add package StackExchange.Redis
cd ..

# No projeto Application (business logic)
cd AiUberMarketplace.Application
dotnet add package MediatR
cd ..

# No projeto API (HTTP)
cd AiUberMarketplace.Api
dotnet add package Microsoft.AspNetCore.SignalR
dotnet add package OpenTelemetry.Instrumentation.AspNetCore
cd ..

# Verificar build
dotnet build
```

### Passo 4: Implementar Domain Model

Criar entities baseado no event catalog:

```csharp
// src/AiUberMarketplace.Domain/Entities/Rider.cs
public class Rider
{
    public Guid RiderId { get; set; }
    public Guid UserId { get; set; }
    public string FirstName { get; set; }
    public string LastName { get; set; }
    public string Email { get; set; }
    public string PhoneNumber { get; set; }
    public string RiderType { get; set; } // "regular", "premium"
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;
}

// src/AiUberMarketplace.Domain/Entities/Driver.cs
public class Driver
{
    public Guid DriverId { get; set; }
    public Guid UserId { get; set; }
    public string FirstName { get; set; }
    public string LastName { get; set; }
    public string LicenseNumber { get; set; }
    public DateTime LicenseExpiry { get; set; }
    public bool IsAvailable { get; set; } = false;
    public decimal AverageRating { get; set; } = 0;
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
}

// src/AiUberMarketplace.Domain/Events/DomainEvent.cs
public abstract class DomainEvent
{
    public string EventId { get; set; } = Guid.NewGuid().ToString();
    public DateTime OccurredAt { get; set; } = DateTime.UtcNow;
    public string AggregateId { get; set; }
}

// src/AiUberMarketplace.Domain/Events/RideRequested.cs
public class RideRequestedEvent : DomainEvent
{
    public Guid RideId { get; set; }
    public Guid RiderId { get; set; }
    public decimal PickupLatitude { get; set; }
    public decimal PickupLongitude { get; set; }
    public decimal DropoffLatitude { get; set; }
    public decimal DropoffLongitude { get; set; }
    public string PickupAddress { get; set; }
    public string DropoffAddress { get; set; }
}
```

### Passo 5: Configurar Database Context

```csharp
// src/AiUberMarketplace.Infrastructure/Database/AiUberMarketplaceDbContext.cs
using Microsoft.EntityFrameworkCore;

public class AiUberMarketplaceDbContext : DbContext
{
    public DbSet<Rider> Riders { get; set; }
    public DbSet<Driver> Drivers { get; set; }
    public DbSet<Vehicle> Vehicles { get; set; }
    public DbSet<Trip> Trips { get; set; }
    public DbSet<Payment> Payments { get; set; }

    public AiUberMarketplaceDbContext(DbContextOptions<AiUberMarketplaceDbContext> options)
        : base(options) { }

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        base.OnModelCreating(modelBuilder);

        // Configure schema
        modelBuilder.HasDefaultSchema("public");

        // Configure entity mappings
        modelBuilder.Entity<Rider>().ToTable("rider_profile", "rider");
        modelBuilder.Entity<Driver>().ToTable("driver_profile", "driver");
        // ... mais configurações
    }
}
```

### Passo 6: Criar primeiro teste

```csharp
// src/AiUberMarketplace.Tests/RiderRegistrationTests.cs
using Xunit;

public class RiderRegistrationTests
{
    [Fact]
    public void RegisterRider_WithValidData_ShouldCreateRider()
    {
        // Arrange
        var riderId = Guid.NewGuid();
        var rider = new Rider
        {
            RiderId = riderId,
            FirstName = "João",
            LastName = "Silva",
            Email = "joao@example.com"
        };

        // Act
        // var result = await riderService.RegisterAsync(rider);

        // Assert
        // Assert.NotNull(result);
        // Assert.Equal(riderId, result.RiderId);
    }
}
```

### Passo 7: Executar build

```bash
cd src
dotnet build

# Se sucesso:
# Build succeeded. 0 Warning(s)

# Se houver erros, corrija baseado na mensagem
```

---

## 📚 Documentação para Fase 1

Antes de começar Phase 1, leia:

1. **Architecture Overview**: [docs/01-architecture.md](docs/01-architecture.md)
   - Compreender bounded contexts
   - Entender event flows

2. **Event Catalog**: [docs/04-event-catalog.md](docs/04-event-catalog.md)
   - Cada event que será publicado
   - Payload de cada evento
   - Quem publica e consome

3. **ADRs**: [docs/adrs/](docs/adrs/)
   - Por que C#/.NET 9
   - Por que PostgreSQL + Redis
   - Por que Redis GEO para geospatial

4. **Development Guide**: [DEVELOPMENT.md](DEVELOPMENT.md)
   - Como setup local environment
   - Como correr testes
   - Como correr application

---

## 🎓 Key Learnings from Phase 0

Estes conceitos serão críticos em Phase 1:

### 1. Idempotency
- Driver assignment deve ser idempotente
- Mesmo se houver retry, deve criar apenas 1 trip

```csharp
// Use idempotency keys
public class AcceptOfferRequest
{
    public Guid OfferId { get; set; }
    public Guid IdempotencyKey { get; set; } // Previne duplicatas
}
```

### 2. Race Conditions
- Dois riders não podem ser assign ao mesmo driver
- Use pessimistic locking ou optimistic locking

```csharp
// Optimistic locking com EF Core
public class Trip
{
    public Guid TripId { get; set; }
    [Timestamp]
    public byte[] RowVersion { get; set; } // Detecta conflitos
}
```

### 3. Event Sourcing Foundation
- Todos os eventos são persistidos
- Reconstituir estado de eventos

```csharp
// Transactional Outbox pattern
using var transaction = await dbContext.Database.BeginTransactionAsync();
try
{
    // 1. Criar trip
    dbContext.Trips.Add(trip);
    
    // 2. Registrar evento
    dbContext.OutboxEvents.Add(new OutboxEvent { ... });
    
    await dbContext.SaveChangesAsync();
    await transaction.CommitAsync();
}
```

### 4. Geospatial Queries
- Redis GEORADIUS para find nearby drivers
- Query é O(N+log(M)) - aceitável para MVP

```csharp
// Redis GEO commands
var nearbyDrivers = await redis.GeoRadiusAsync(
    key: "drivers:active",
    longitude: rideRequest.PickupLongitude,
    latitude: rideRequest.PickupLatitude,
    radius: 5, // 5 km
    unit: GeoUnit.Kilometers
);
```

### 5. Observability
- Rastrear cada operação com correlation ID
- Logar eventos importantes

```csharp
// Structured logging
logger.LogInformation(
    "Ride accepted: {RideId} by {DriverId}",
    rideId,
    driverId);

// Distributed tracing
using var activity = Activity.StartActivity("AcceptRide");
activity?.SetTag("ride.id", rideId);
activity?.SetTag("driver.id", driverId);
```

---

## ⚠️ Armadilhas Comuns em Phase 1

1. **Não testar race conditions**
   - Implementar testes concorrentes
   - Simular 2 drivers aceitando mesmo ride

2. **Esquecer idempotency**
   - Payment pode ser retentada
   - Mesmo request não deve criar 2 charges

3. **Não logar eventos**
   - Será impossível debugar issues depois
   - Rastrear cada mudança de state

4. **Build sem warning**
   - Compilador .NET tem warnings úteis
   - Use `dotnet build -warnaserror`

5. **Não validar entrada**
   - Validar email, phone, coordinates
   - Use FluentValidation

---

## 📞 Contactar com Dúvidas

Se algo não estiver claro:

1. **Re-ler o ADR relevant**: [docs/adrs/](docs/adrs/)
2. **Consultar Event Catalog**: [docs/04-event-catalog.md](docs/04-event-catalog.md)
3. **Verificar Architecture**: [docs/01-architecture.md](docs/01-architecture.md)
4. **Testar local**: `docker-compose logs -f <service>`

---

## ✅ Checklist: Pronto para Phase 1?

- [ ] Phase 0 documentação revisada
- [ ] Docker Compose rodando sem erros
- [ ] PostgreSQL acessível e schema criado
- [ ] Redis conectando e respondendo
- [ ] .NET 9 SDK instalado e verificado
- [ ] Node.js 18+ instalado
- [ ] Repository clonado localmente
- [ ] Documentação lida (README, docs/, ADRs)
- [ ] Preparado para implementar ride lifecycle

**Quando estiver pronto, proceda com Phase 1!**

---

## 🎯 Phase 1 Objetivo Final

Ao final de Phase 1, deve ser possível:

```bash
# 1. Registrar rider
curl -X POST http://localhost:5000/api/riders/register \
  -d '{"firstName":"João","lastName":"Silva","email":"joao@example.com"}'

# 2. Registrar driver
curl -X POST http://localhost:5000/api/drivers/register \
  -d '{"firstName":"Maria","lastName":"Santos","licenseNumber":"ABC123"}'

# 3. Solicitar ride
curl -X POST http://localhost:5000/api/rides/request \
  -d '{"riderId":"...", "pickupLat":-23.55, "pickupLng":-46.63}'

# 4. Driver aceita ride
curl -X POST http://localhost:5000/api/rides/{rideId}/accept \
  -d '{"driverId":"..."}'

# 5. Iniciar trip
curl -X POST http://localhost:5000/api/trips/{tripId}/start

# 6. Completar trip
curl -X POST http://localhost:5000/api/trips/{tripId}/complete \
  -d '{"fare":25.50}'
```

E ter 80%+ test coverage com testes de concurrency!

---

**Boa sorte! Phase 1 aguarda! 🚀**
