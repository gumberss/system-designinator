# AI Uber Marketplace - Índice Completo de Documentação

## 📑 Documentação Principal (Leitura Obrigatória)

### 1. **[README.md](README.md)** - Começar aqui! ⭐
   - Visão geral do projeto e objetivos
   - Todas as 10 fases explicadas
   - Quick start local
   - Estrutura de repositório
   - **Leia primeiro (10 min)**

### 2. **[DEVELOPMENT.md](DEVELOPMENT.md)** - Setup Local
   - Pré-requisitos (Docker, .NET, Node.js)
   - Passo-a-passo de instalação
   - Como rodar cada serviço
   - Troubleshooting e dicas
   - **Leia quando for começar desenvolvimento (20 min)**

### 3. **[PHASE0-SUMMARY.md](PHASE0-SUMMARY.md)** - Sumário de Entrega
   - O que foi completado em Phase 0
   - Estatísticas de documentação e infraestrutura
   - Decisões arquiteturais resumidas
   - Status de aceitação de critérios
   - **Leia para entender o que Phase 0 entregou (15 min)**

### 4. **[NEXT-STEPS.md](NEXT-STEPS.md)** - Phase 1
   - Como validar Phase 0
   - Passo-a-passo para Phase 1
   - Conceitos chave que serão usados
   - Armadilhas comuns
   - **Leia antes de começar Phase 1 (20 min)**

---

## 🏗️ Documentação de Arquitetura

### 5. **[docs/01-architecture.md](docs/01-architecture.md)** - Arquitetura Completa ⭐
   - Diagrama C4 (Context, Container)
   - 14 Bounded Contexts explicados
   - Stack tecnológico justificado
   - Cálculos de escala (231 RPS, 750K GPS/sec)
   - Modelo de consistência
   - Cenários de falha
   - **Essencial (30 min)**

### 6. **[docs/00-phase0-acceptance-criteria.md](docs/00-phase0-acceptance-criteria.md)** - Critérios Phase 0
   - Checklist detalhado de aceitação
   - Validação de cada deliverable
   - Como revisar Phase 0
   - **Consulte para validar conclusão (10 min)**

### 7. **[docs/04-event-catalog.md](docs/04-event-catalog.md)** - Catálogo de Eventos ⭐
   - 20+ eventos do domínio
   - Schema de cada evento com payload
   - Quem publica e consome
   - Guarantias de entrega
   - Ordenação crítica
   - Idempotency keys
   - **Essencial para implementação (25 min)**

---

## 🎯 Architecture Decision Records (ADRs)

Cada ADR documenta UMA decisão importante. **Leia quando quiser entender POR QUÊ algo foi escolhido.**

### 8. **[docs/adrs/README.md](docs/adrs/README.md)** - Guia de ADRs
   - Como ler um ADR
   - Estrutura padrão
   - Definição de status
   - Princípios de decisão
   - **Leia primeiro (5 min)**

### 9. **[docs/adrs/0001-backend-language.md](docs/adrs/0001-backend-language.md)** - C#/.NET 9 ⭐
   - Por que C# e não Go, Python, Java, Node.js, Rust?
   - Async/await é o gold standard
   - Strong type system
   - SignalR para WebSockets
   - Trade-offs e consequências
   - **Importante entender (15 min)**

### 10. **[docs/adrs/0002-database-architecture.md](docs/adrs/0002-database-architecture.md)** - PostgreSQL + Redis + Cassandra ⭐
   - Por que polyglot architecture?
   - PostgreSQL para transações (231 RPS)
   - Redis para location ingestion (750K/sec)
   - Cassandra para histórico
   - Quando usar cada database
   - Trade-offs da complexidade operacional
   - **Fundamental (20 min)**

### 11. **[docs/adrs/0003-geospatial-indexing.md](docs/adrs/0003-geospatial-indexing.md)** - Estratégia Geospatial
   - Redis GEO vs Quadtree
   - Por que começar simples (Redis GEO)
   - Quando migrar para Quadtree (Phase 5)
   - Comparações de performance
   - Implementação fase-por-fase
   - **Crítico para Phase 2 (15 min)**

---

## 🔧 Configuração & Infraestrutura

### 12. **[docker-compose.yml](docker-compose.yml)** - Orquestração Local
   - 8 serviços configurados:
     - PostgreSQL 16
     - Redis 7
     - Redpanda (Kafka)
     - Jaeger (tracing)
     - Prometheus (métricas)
     - Grafana (dashboards)
     - pgAdmin (DB UI)
     - Redpanda Console (Kafka UI)
   - Health checks configurados
   - Volumes persistentes
   - **Use para `docker-compose up -d`**

### 13. **[infra/postgres/init.sql](infra/postgres/init.sql)** - Schema PostgreSQL
   - 10 schemas criados:
     - identity, rider, driver, location, trip, payment, fraud, rating, event_store, audit
   - Todas as tabelas
   - Índices otimizados
   - PostGIS habilitado
   - Event store para outbox pattern
   - **Executado automaticamente no container**

### 14. **[infra/prometheus/prometheus.yml](infra/prometheus/prometheus.yml)** - Configuração Prometheus
   - Scrape configs para todos os serviços
   - Intervalos de coleta
   - Targets de jobs
   - **Usado por docker-compose**

---

## 📂 Estrutura de Repositório

```
ai-uber-marketplace/
│
├─ README.md                          # 📄 Comece aqui
├─ DEVELOPMENT.md                     # 📄 Setup local
├─ PHASE0-SUMMARY.md                  # 📄 Resumo Phase 0
├─ NEXT-STEPS.md                      # 📄 Phase 1
│
├─ docs/
│  ├─ 01-architecture.md              # 🏗️  Arquitetura completa
│  ├─ 04-event-catalog.md             # 📋 Eventos do domínio
│  ├─ 00-phase0-acceptance-criteria.md # ✅ Critérios Phase 0
│  │
│  └─ adrs/
│     ├─ README.md                    # 📖 Guia de ADRs
│     ├─ 0001-backend-language.md     # 🎯 C#/.NET 9
│     ├─ 0002-database-architecture.md # 🎯 PostgreSQL + Redis
│     └─ 0003-geospatial-indexing.md  # 🎯 Estratégia Geospatial
│
├─ src/                               # 💻 Backend .NET
│  ├─ AiUberMarketplace.Api
│  ├─ AiUberMarketplace.Domain
│  ├─ AiUberMarketplace.Application
│  ├─ AiUberMarketplace.Infrastructure
│  └─ AiUberMarketplace.Web
│
├─ frontend/                          # 💻 Frontend React + TypeScript
│  └─ src/
│
├─ tests/                             # 🧪 Test projects
│  ├─ AiUberMarketplace.Tests
│  └─ AiUberMarketplace.IntegrationTests
│
├─ infra/                             # 🔧 Infrastructure
│  ├─ postgres/
│  │  └─ init.sql                     # 🗄️  Database schema
│  ├─ prometheus/
│  │  └─ prometheus.yml               # 📊 Metrics config
│  └─ grafana/
│     ├─ dashboards/
│     └─ datasources/
│
└─ docker-compose.yml                 # 🐳 Local environment
```

---

## 🎓 Caminhos de Leitura Recomendados

### 👤 Para Product Managers / Stakeholders (30 min)
1. README.md
2. docs/01-architecture.md (seções: Vision, Scale Assumptions)
3. PHASE0-SUMMARY.md

### 👨‍💻 Para Desenvolvedores .NET (90 min)
1. README.md
2. DEVELOPMENT.md
3. docs/01-architecture.md (completo)
4. docs/04-event-catalog.md (Ride events)
5. docs/adrs/0001-backend-language.md
6. docs/adrs/0002-database-architecture.md
7. NEXT-STEPS.md (Phase 1 section)

### 🏗️ Para Arquitetos (120 min)
1. Todos os arquivos acima
2. docs/adrs/0003-geospatial-indexing.md
3. Estudar diagrama C4 em docs/01-architecture.md
4. Revisar bounded contexts
5. Validar critérios em docs/00-phase0-acceptance-criteria.md

### 🚀 Para Começar Phase 1 (60 min)
1. DEVELOPMENT.md
2. NEXT-STEPS.md
3. docs/04-event-catalog.md (todos eventos)
4. docs/adrs/0002-database-architecture.md (seção: implementation)
5. Executar: `docker-compose up -d` e validar serviços

---

## 📊 Quick Reference

### 🔑 Conceitos-Chave

| Conceito | Documentado em | Importância |
|----------|---|---|
| Bounded Contexts (14 total) | docs/01-architecture.md | ⭐⭐⭐ |
| Event Catalog (20+ eventos) | docs/04-event-catalog.md | ⭐⭐⭐ |
| C#/.NET 9 Decision | docs/adrs/0001-*.md | ⭐⭐ |
| Database Strategy | docs/adrs/0002-*.md | ⭐⭐⭐ |
| Geospatial Strategy | docs/adrs/0003-*.md | ⭐⭐ (Phase 2) |
| Scale Calculations | docs/01-architecture.md | ⭐⭐ |
| Local Development | DEVELOPMENT.md | ⭐⭐⭐ |

### 💡 Respostas Rápidas

**P: Por que C#/.NET?**  
R: Ver [ADR-0001](docs/adrs/0001-backend-language.md) - async/await gold standard, SignalR para WebSockets

**P: Por que PostgreSQL + Redis + Cassandra?**  
R: Ver [ADR-0002](docs/adrs/0002-database-architecture.md) - polyglot para diferentes workloads

**P: Como começo a desenvolver?**  
R: Ver [DEVELOPMENT.md](DEVELOPMENT.md) - passo-a-passo com `docker-compose up`

**P: Quais eventos existem?**  
R: Ver [docs/04-event-catalog.md](docs/04-event-catalog.md) - 20+ eventos com payloads

**P: Qual a arquitetura?**  
R: Ver [docs/01-architecture.md](docs/01-architecture.md) - C4 diagrams e bounded contexts

**P: O que é Phase 1?**  
R: Ver [NEXT-STEPS.md](NEXT-STEPS.md) - Ride lifecycle vertical slice

---

## ✅ Checklist de Leitura (Antes de Code)

- [ ] Li README.md
- [ ] Li DEVELOPMENT.md e rodei `docker-compose up -d`
- [ ] Li docs/01-architecture.md
- [ ] Li docs/04-event-catalog.md
- [ ] Li pelo menos 2 ADRs (0001 e 0002)
- [ ] Entendo os 14 bounded contexts
- [ ] Sei qual é o objetivo de Phase 1
- [ ] Rodei `docker-compose ps` e todos os serviços estão healthy

**Quando todos estiverem marcados: ✅ Pronto para Phase 1!**

---

## 🔗 Links Úteis (Dentro do Projeto)

- **Começar**: [README.md](README.md)
- **Setup**: [DEVELOPMENT.md](DEVELOPMENT.md)
- **Arquitetura**: [docs/01-architecture.md](docs/01-architecture.md)
- **Eventos**: [docs/04-event-catalog.md](docs/04-event-catalog.md)
- **Decisões**: [docs/adrs/](docs/adrs/)
- **Próximos Passos**: [NEXT-STEPS.md](NEXT-STEPS.md)

---

## 📞 Suporte

Não conseguiu encontrar resposta?

1. Procure pela palavra-chave em todos os arquivos `.md`
2. Consulte o ADR relevante na pasta `docs/adrs/`
3. Verifique `docs/04-event-catalog.md` para eventos
4. Releia `docs/01-architecture.md` - provavelmente responde

---

**Última atualização**: 2026-11-11  
**Versão**: Phase 0 - Architecture Foundation Complete ✅  
**Status**: Pronto para Phase 1 🚀
