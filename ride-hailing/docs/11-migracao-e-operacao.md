# 11. Schema inicial e operacao local

Este laboratorio nao carrega dados de uma versao anterior, nao transforma
schemas existentes e nao inclui migrations. O schema completo esta em
[001-schema.sql](../infra/001-schema.sql); ele e aplicado pelo entrypoint
PostgreSQL somente quando `/var/lib/postgresql/data` e inicializado vazio.
Seeds sao apenas fixtures didaticas e podem ser recriados junto com o banco.

## Recriar o ambiente local

O comando abaixo e destrutivo para os dados do projeto: apaga corridas,
idempotency keys, eventos, trajetos, checkpoints SQL e dados Redis.
Ele e intencionalmente limitado ao projeto Compose `ride-hailing`.

```powershell
Set-Location .\ride-hailing
docker compose --profile multi-instance down -v
docker compose build trip-manager
docker compose --profile multi-instance up -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:58100/health/ready
```

Nao executa `docker volume prune` e nao remove volumes de outros projetos.
`down -v` e uma escolha explicita para este laboratorio, nao um procedimento
recomendado para banco de producao. Em producao, versionar migrations e preparar
backup, rollout compativel e rollback.

## Inicializacao e atualizacao

```powershell
Set-Location .\ride-hailing
npm ci
npm run build
docker compose build trip-manager
docker compose up -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:58100/health/ready
```

Compose aguarda PostgreSQL e Redis saudaveis antes dos processos que dependem deles.
O gateway aguarda readiness das request replicas e do writer.
O request readiness consulta trip manager, location manager, map e finder.
Isso verifica dependencias basicas, nao prova capacidade nem ausencia de backlog.

Processos possuem restart unless-stopped, init e shutdown de HTTP, subscriber,
Redis e pool. Graceful shutdown local nao substitui draining distribuido.
Nginx usa resolver Docker com validade ~5s para containers recriados.

## Budget local

| Limite | Valor / comportamento |
|---|---|
| Hash por actor | 10.000 identidades; novas acima do limite recebem 503 |
| Backlog stream | 100.000 amostras; sem truncamento para fingir sucesso |
| Candidatos/delta | 2000 IDs; excedente devolve 503 |
| Corpo JSON | 64KB |
| Requisicoes em voo | 64 por servico, separado de SSE |
| SSE | 200 de estado; 200 por actor de GPS |
| Pool PostgreSQL | max 10 por processo que usa SQL |
| Timeout HTTP interno | 4s |
| Timeout de comando Redis | 3s |
| Quadtree | capacidade 32, maxDepth 16; refresh ~15s, idade maxima 30s |
| Writer | ate 2000 pontos por lote, ate 8 lotes por ciclo de ~15s |

Os hashes e delta nao sao varridos automaticamente para apagar identidade:
reter watermark evita aceitar pacote antigo depois de esquecer a sequencia.
Producao precisa epoch/retencao e particionamento, nao simplesmente aumentar caps.

## Durabilidade e falhas

Redis e uma instancia com AOF everysec e noeviction.
ACK de ingestao nao tem a mesma durabilidade de commit SQL.
Perder Redis antes do flush pode perder pontos nao persistidos.
Writer parado aumenta backlog ate ocorrer backpressure, em vez de truncar.
RPO/RTO, replicacao e restore nao foram certificados.

Outbox de estado e duravel no PostgreSQL mesmo sem Redis.
Publicacao pode duplicar; leitores deduplicam por cursor e versao.
Pub/sub GPS nao tem replay; o hash permite retomar ultimo valor.
Trajeto e recuperado do PostgreSQL apos lote, com cursor de sequencia paginado.

## Superficie de acesso

Somente gateway, Redis e PostgreSQL publicam portas no host, em loopback.
Servicos internos nao possuem portas publicadas e /internal nao passa no gateway.
Isso reduz exposicao, mas nao e autenticacao forte: containers da mesma rede
podem acessar endpoints internos. Producao precisa identidade de servico e ACL.

Gateway nao reexecuta POST automaticamente. Retry pertence ao cliente com
Idempotency-Key original. 502/504 podem ocorrer no gateway; nao inferir que
nenhum pedido foi persistido.

## Limpeza

`docker compose --profile multi-instance stop` para servicos sem apagar dados.
Use `down -v` somente quando quiser reiniciar o laboratorio do zero.
Os testes removem somente schema/prefixo gerados para a execucao.
