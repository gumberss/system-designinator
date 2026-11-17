# Development Setup Guide

This guide walks you through setting up the AI Uber Marketplace project for local development.

## Prerequisites

### System Requirements
- Windows 11 / macOS 12+ / Ubuntu 20.04 LTS
- 16 GB RAM (minimum, 32 GB recommended)
- 50 GB free disk space
- CPU with virtualization support (for Docker)

### Software Requirements

#### Required
- **Docker Desktop** 4.10+ ([Install](https://www.docker.com/products/docker-desktop))
- **.NET 9 SDK** ([Install](https://dotnet.microsoft.com/download/dotnet/9.0))
- **Node.js 18+** ([Install](https://nodejs.org/))
- **Git** ([Install](https://git-scm.com/))
- **Visual Studio Code** or **Visual Studio 2022**

#### Optional but Recommended
- **pgAdmin** for database management
- **Redis CLI** for manual Redis operations
- **Kafka CLI tools** for message queue debugging
- **curl** or **Postman** for API testing

## Installation Steps

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/system-designinator.git
cd system-designinator/ai-uber-marketplace
```

### 2. Verify Prerequisites

```bash
# Check .NET SDK
dotnet --version
# Output should be: 9.0.0 or higher

# Check Node.js
node --version
npm --version

# Check Docker
docker --version
docker-compose --version
```

### 3. Start Infrastructure Services

```bash
# Start all services (PostgreSQL, Redis, Redpanda, Jaeger, Prometheus, Grafana)
docker-compose up -d

# Verify all services are healthy
docker-compose ps

# Check logs of specific service
docker-compose logs postgres
docker-compose logs redis
```

**Expected Output**:
```
NAME                 COMMAND              STATUS          PORTS
aiuber-postgres      postgres             Up (healthy)    5432
aiuber-redis         redis-server         Up (healthy)    6379
aiuber-redpanda      redpanda start       Up (healthy)    9092, 29092
aiuber-jaeger        /go/bin/all-in-one   Up              16686
aiuber-prometheus    prometheus           Up              9090
aiuber-grafana       /run.sh              Up              3000
pgadmin              /entrypoint.sh       Up              5050
```

**Troubleshooting**:
```bash
# If containers fail to start, check logs
docker-compose logs -f

# Reset everything (careful - deletes data!)
docker-compose down -v
docker-compose up -d
```

### 4. Access Services

Once all services are healthy, you can access:

| Service | URL | Credentials |
|---------|-----|-----------|
| **Redpanda Console** | http://localhost:8080 | - |
| **pgAdmin** | http://localhost:5050 | admin@aiuber.local / admin |
| **Jaeger UI** | http://localhost:16686 | - |
| **Prometheus** | http://localhost:9090 | - |
| **Grafana** | http://localhost:3000 | admin / admin |

**PostgreSQL Connection**:
```
Host: localhost
Port: 5432
User: aiuber_user
Password: aiuber_password
Database: aiuber_db
```

**Redis Connection**:
```bash
redis-cli -h localhost -p 6379 -n 0
```

### 5. Build Backend Solution

```bash
cd src

# Restore NuGet packages
dotnet restore

# Build solution
dotnet build

# Expected output: "Build succeeded. 0 Warning(s)"
```

### 6. Build Frontend Application

```bash
cd frontend

# Install dependencies
npm install

# Build for development
npm run build

# Expected output: "dist/index.html"
```

### 7. Run Migrations (Phase 1+)

```bash
cd src/AiUberMarketplace.Infrastructure

# Apply database migrations
dotnet ef database update

# Verify tables created
dotnet ef migrations list
```

**Note**: Phase 0 has no migrations. The PostgreSQL initialization script handles schema creation.

## Development Workflow

### Running Backend API

```bash
cd src/AiUberMarketplace.Api

# Run with hot reload
dotnet watch run

# Output should show:
# info: Microsoft.AspNetCore.Hosting.Diagnostics
#       Application started. Press Ctrl+C to quit.
#       
# Listening on http://localhost:5000
# Listening on https://localhost:5001
```

**Health Check**:
```bash
curl http://localhost:5000/health
```

### Running Frontend Development Server

```bash
cd frontend

# Start Vite development server
npm run dev

# Output should show:
#   ➜  Local:   http://localhost:5173/
#   ➜  press h to show help
```

### Running Tests

```bash
cd src

# Run all tests
dotnet test

# Run specific test project
dotnet test AiUberMarketplace.Tests

# Run with verbose output
dotnet test -v n

# Run specific test class
dotnet test --filter "FullyQualifiedName~RideRequestTests"
```

### Database Queries

#### Using pgAdmin (Web UI)
1. Open http://localhost:5050
2. Login: admin@aiuber.local / admin
3. Add server:
   - Host: postgres
   - Port: 5432
   - Username: aiuber_user
   - Password: aiuber_password

#### Using Command Line
```bash
# Connect to PostgreSQL
psql -h localhost -U aiuber_user -d aiuber_db

# List schemas
\dn

# List tables in schema
\dt identity.*

# Query rider data
SELECT * FROM rider.rider_profile LIMIT 10;

# Exit
\q
```

#### Using Redis CLI
```bash
# Connect
redis-cli -h localhost

# Set a key
SET test:key "Hello World"

# Get the key
GET test:key

# List keys
KEYS *

# Exit
quit
```

## Common Commands

```bash
# Start all infrastructure
docker-compose up -d

# Stop all services (keep data)
docker-compose stop

# Start services again
docker-compose start

# Remove all containers and volumes (DELETE DATA!)
docker-compose down -v

# View logs of specific service
docker-compose logs -f postgres

# Execute command in running container
docker-compose exec postgres psql -U aiuber_user -d aiuber_db

# Rebuild specific service
docker-compose up -d --build postgres

# SSH into container
docker-compose exec postgres /bin/bash
```

## IDE Setup

### Visual Studio Code

1. Install extensions:
   - C# Dev Kit
   - Docker
   - REST Client
   - PostgreSQL
   - Redis

2. Open workspace:
   ```bash
   code .
   ```

3. Press `Ctrl+Shift+D` to open debugger, create `.vscode/launch.json`:
   ```json
   {
     "version": "0.2.0",
     "configurations": [
       {
         "name": ".NET 9 Launch",
         "type": "coreclr",
         "request": "launch",
         "preLaunchTask": "build",
         "program": "${workspaceFolder}/src/AiUberMarketplace.Api/bin/Debug/net9.0/AiUberMarketplace.Api.dll",
         "args": [],
         "cwd": "${workspaceFolder}/src/AiUberMarketplace.Api",
         "stopAtEntry": false,
         "console": "internalConsole"
       }
     ]
   }
   ```

### Visual Studio 2022

1. Open `src/AiUberMarketplace.sln`
2. Set `AiUberMarketplace.Api` as startup project
3. Press F5 to run with debugging

## Troubleshooting

### "Docker daemon not running"
```bash
# Start Docker Desktop or Docker daemon
docker-compose ps  # Verify it's running
```

### "Connection refused" on PostgreSQL
```bash
# Check if PostgreSQL service is healthy
docker-compose ps postgres

# View logs
docker-compose logs postgres

# Restart service
docker-compose restart postgres

# Wait a few seconds and retry connection
```

### "Redis connection failed"
```bash
# Check Redis service
docker-compose ps redis

# Test Redis connection
redis-cli -h localhost ping
# Should respond: PONG
```

### ".NET build fails: missing NuGet packages"
```bash
# Clear cache and restore
dotnet nuget locals all --clear
dotnet restore
```

### "Port already in use"
```bash
# Find process using port 5432 (PostgreSQL)
lsof -i :5432

# Kill process (macOS/Linux)
kill -9 <PID>

# Or change port in docker-compose.yml
# ports: "5433:5432"  # Use 5433 instead
```

### "npm packages not installing"
```bash
# Clear npm cache
npm cache clean --force

# Delete node_modules and reinstall
rm -rf node_modules package-lock.json
npm install
```

## Environment Variables

Create `.env` file in project root:

```env
# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=aiuber_user
POSTGRES_PASSWORD=aiuber_password
POSTGRES_DB=aiuber_db

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Kafka/Redpanda
KAFKA_BROKERS=localhost:29092

# Jaeger
JAEGER_AGENT_HOST=localhost
JAEGER_AGENT_PORT=6831

# API
API_PORT=5000
API_ENVIRONMENT=Development
```

## Next Steps

1. **Explore the architecture**: Read [docs/01-architecture.md](docs/01-architecture.md)
2. **Review ADRs**: Check [docs/adrs/](docs/adrs/)
3. **Understand domain events**: See [docs/04-event-catalog.md](docs/04-event-catalog.md)
4. **Run tests**: `dotnet test` in `/src`
5. **Make your first change**: Create a feature branch and submit a PR

## Tips & Best Practices

### Local Development
- Always run `docker-compose up -d` first
- Use `dotnet watch run` for auto-reload
- Keep npm dev server running in separate terminal
- Use `npm run build` before committing frontend changes

### Version Control
- Create feature branch: `git checkout -b feature/my-feature`
- Commit with meaningful messages
- Include ADR reference in commits: "feat: implement driver location update (ADR-0003)"
- Push to remote: `git push origin feature/my-feature`
- Create Pull Request with description

### Code Quality
- Format code: `dotnet format`
- Run tests: `dotnet test`
- Check linting: `npm run lint` (frontend)
- No warnings on build: `dotnet build -warnaserror`

### Debugging
- Use breakpoints in IDE
- Check logs in `docker-compose logs`
- Use Jaeger UI for tracing: http://localhost:16686
- Use Prometheus for metrics: http://localhost:9090

## Documentation

- [Architecture Overview](docs/01-architecture.md)
- [Event Catalog](docs/04-event-catalog.md)
- [Architecture Decisions](docs/adrs/)
- [API Contracts](docs/05-api-contracts.md) (Phase 1+)

## Support

If you encounter issues:

1. Check this guide for common problems
2. Review Docker logs: `docker-compose logs -f`
3. Restart services: `docker-compose restart`
4. Create an issue on GitHub with:
   - Clear description
   - Steps to reproduce
   - Error messages
   - Your environment (OS, Docker version, .NET version)

## Performance Tips

### Docker Optimization
- Allocate 8GB+ RAM to Docker Desktop
- Use SSD for volume mounts
- Monitor resource usage in Docker Desktop settings

### Database Optimization
- Enable query logging in PostgreSQL
- Monitor slow queries: `EXPLAIN ANALYZE`
- Create indexes for frequently queried fields

### Redis Optimization
- Monitor memory usage
- Set TTL on temporary data
- Use Redis pipelining for bulk operations

---

**Happy Coding!** 🚀

For updates to this guide, see the repository wiki or create an issue.
