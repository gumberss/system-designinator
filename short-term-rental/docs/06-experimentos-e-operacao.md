# 6. Aprender rodando: experimentos e operacao

## 1. Testes rapidos sem infraestrutura

```powershell
dotnet build short-term-rental.sln -c Release
dotnet test tests\ShortTermRental.Tests\ShortTermRental.Tests.csproj -c Release --no-build
```

Testam a validacao de datas, duracao maxima, IDs e retries historicos.
Nao provam concorrencia do PostgreSQL. Para isso, rode `verify` com infraestrutura.

## 2. Disputa e invariantes

```powershell
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- verify --attempts 200 --concurrency 64
```

Verificacoes:

- 200 pedidos de guests diferentes: 1 criado + 199 conflitos.
- Disponibilidade muda depois do commit.
- 32 retries da chave existente: mesmo bookingId.
- 32 pedidos simultaneos com chave nova: 1 criado + 31 replays.
- Mesma chave/payload diferente: conflito.
- Intervalos contidos, parciais e envolvendo a reserva: conflito.
- Intervalos adjacentes: permitidos.
- SQL direto contornando a API: exclusion violation.
- Datas vazias, chave ausente, imovel inexistente: 400/404.
- Cancelamento repetido: sem ressuscitar reserva ou duplicar efeito.
- 24 imoveis diferentes: todos confirmam em paralelo.
- Outbox/inbox/projecao convergem em ate 30s, sem overlaps.
- Duplicata verdadeira aumenta deliveries, mas nao o efeito.
- Poison JSON chega a DLQ.

O verificador falha com exit code 1 se uma assercao nao passar.
Faz limpeza por cancelamento dos IDs que criou; nao apaga dados de outras execucoes.
Janelas futuras aleatorias evitam colisoes entre execucoes normais, mas nao sao
prova contra qualquer dataset preexistente.

## 3. Duas APIs

```powershell
docker compose --profile multi-instance up -d --build
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- verify --second-url http://localhost:58081
```

Observe que a garantia nao depende da memoria de um processo. Cada instancia
tambem roda publisher e consumer: `SKIP LOCKED` e inbox lidam com concorrencia.
Os limites HTTP/pools sao por instancia, nao globais.

## 4. Ler relatorios de carga

`load` e **closed-loop**: cada worker espera a resposta antes da proxima request.
Se o sistema fica lento, ele gera menos requests/s. Isso mede esse arranjo
cliente-servidor, nao injeta uma taxa fixa independente da latencia.
Ha coordinated omission: o teste pode subestimar o sofrimento de chegadas
continuas em saturacao. Um teste de capacidade serio deve complementar com
open-loop e geradores separados, sem competir pelo mesmo CPU.

- `read-percent`: probabilidade por tentativa; 90/10 nao garante 90M/10M RPS.
- `hotspot-percent`: probabilidade de escrita contra mesmo imovel/mesmas datas.
- Sem hotspot: IDs/datas disjuntos dentro da execucao. O setup le o maior checkout
  do historico e inicia a janela no dia seguinte; assim cargas sequenciais nao
  colidem acidentalmente. Essa consulta direta nao entra na medicao.
- 409 esperado em hotspot nao e falha de integridade, mas nao e sucesso de booking.
- 429 e backpressure; contabilizado separado, nao "request atendida com sucesso".
- p50/p95/p99 sao latencias **combinadas** de reads/writes, inclusive conflitos.
- `confirmedWritesPerSecond` conta somente 201.
- `peakClientRequestsInFlight` mede o pico de operacoes HTTP pendentes no gerador,
  da chamada ate a leitura da resposta ou erro. Pode incluir espera de conexao/rede;
  nao mede quantas queries executavam simultaneamente no PostgreSQL.
- Pool/conexoes cliente, servidor e Docker usam a mesma maquina.
- Startup/cache frio, GC, duracao curta e dataset pequeno influenciam resultados.

O banco de `SHORT_TERM_RENTAL_POSTGRES` deve ser o mesmo da API. Nao executar duas
cargas simultaneamente: a escolha de janela e um setup de laboratorio, nao
um alocador distribuido de datas. Com hotspot=0, um 409 e considerado inesperado
e faz a carga falhar, em vez de inflar a taxa de "escritas" com conflitos.

O gerador termina novas tentativas no deadline e aguarda as em voo (timeout 30s);
elapsedSeconds pode ser maior que duration. Nao cancela requests no instante
final e depois interpreta cancelamento como falha da API.

Retencao do array de latencias e outro limite do gerador. Para milhoes/bilhoes
de amostras, usar histogramas de memoria limitada, metricas separadas por
operacao e testes distribuidos.

## 5. Inspecionar o banco

```powershell
docker compose exec -T postgres psql -U short-term-rental -d short-term-rental -c "SELECT status, count(*) FROM bookings GROUP BY status;"
docker compose exec -T postgres psql -U short-term-rental -d short-term-rental -c "SELECT count(*) AS pending FROM outbox WHERE published_at IS NULL;"
docker compose exec -T postgres psql -U short-term-rental -d short-term-rental -c "SELECT event_id, deliveries FROM processed_events WHERE deliveries > 1 ORDER BY processed_at DESC LIMIT 10;"
```

Diagnostico HTTP:

```powershell
Invoke-RestMethod http://localhost:58080/diagnostics
```

`pendingEvents=0` nao basta: evento publicado pode aguardar consumo.
Em sistema ocioso, esperar tambem `processedEvents=totalEvents` e
`projectedActiveBookings=confirmedBookings`.
`overlappingPairs` deve sempre ser zero.

O self-join de overlap e uma verificacao educativa cujo custo depende do plano,
dataset e uso do GiST; nao e algo para expor a todos numa carga global.

Se a taxa de escrita superar a de consumo, outbox pode estar vazia enquanto a
fila ainda acumula milhares de eventos. A primeira carga deste laboratorio
evidenciou exatamente isso com o dispatch sequencial do cliente RabbitMQ.
A implementacao usa agora 8 callbacks concorrentes por instancia, prefetch 16,
ACK individual e transacoes independentes. Compare throughput e tempo de
convergencia, nao apenas a taxa HTTP.

## 6. Falha de RabbitMQ em runtime

Para executar a matriz principal automaticamente:

```powershell
powershell -NoProfile -File scripts\Test-Failures.ps1
```

Requer Docker CLI/Compose e stack ja iniciada. Interrompe temporariamente os
servicos **deste projeto**; nao rode junto de outros testes ou uso do laboratorio.
Os exercicios abaixo permitem observar cada passo manualmente.

Com API ja iniciada e saudavel:

```powershell
docker compose stop rabbitmq
# Use o POST manual do README em uma janela livre.
Invoke-RestMethod http://localhost:58080/diagnostics
docker compose start rabbitmq
docker compose logs --tail 50 api
Invoke-RestMethod http://localhost:58080/diagnostics
```

Reserva deve continuar confirmando no banco; pendingEvents cresce e converge
depois de recuperar o broker. Logs devem mostrar falha e retry, nao sucesso falso.
Nao use `compose up` com broker parado para este exercicio: depends_on bloquearia
o startup, que e uma condicao diferente de falha em runtime.

## 7. Redis fora

```powershell
docker compose stop redis
Invoke-RestMethod 'http://localhost:58080/properties?limit=1'
# Pegue um ID e chame GET /properties/{id}: este endpoint usa o cache.
docker compose start redis
```

O endpoint de lista nao usa Redis. Para exercitar fallback, use o endpoint do
imovel individual. Warning deve aparecer; resposta vem do PostgreSQL.
Mais cache misses aumentam custo do banco: fallback nao e capacidade gratuita.

## 8. PostgreSQL fora

```powershell
docker compose stop postgres
# GET /health/ready ou POST /bookings deve falhar, nao confirmar.
docker compose start postgres
```

Clientes PowerShell lancam excecao para HTTP 503; e esperado.
Se POST sofreu timeout, repetir mesmo payload/chave depois de recuperar.
Nao deduzir rollback apenas por falha de rede.

## 9. Alertas que seriam obrigatorios em producao

Latencia p99 por endpoint, erro/timeout, tempo de pool, locks/deadlocks,
throughput **confirmado**, cache hit/miss, WAL/storage, idade e tamanho da
outbox, fila ready/unacked, DLQ, redeliveries e divergencia de projecao.

Logs estruturados de falhas existem no ASP.NET e workers. Nao ha stack completa
de Prometheus/OpenTelemetry/dashboard: diagnosticos nao substituem observabilidade.

## 10. Evolucoes para praticar

1. Implementar inventario diario UNIQUE e comparar escrita por noite com GiST.
2. Implementar hold que ocupa inventario, com expiresAt e transicao atomica.
   Nao excluir um hold apenas porque relogio local diz que expirou; a autoridade
   precisa decidir validade e concorrer com confirmacao/pagamento.
3. Separar worker da API e medir efeito de pool/backlog.
4. Criar replica para catalogo, preservando primary para confirmacao.
5. Projetar particionamento por imovel e demonstrar por que duas autoridades
   independentes nao preservam o mesmo invariante.

## Problemas comuns

- **Engine Docker desligado:** iniciar Docker Desktop com Linux containers.
- **Porta ocupada:** ajustar Compose e URLs/conexoes do host, nao matar processos
  de outros projetos. Containers continuam usando portas internas originais.
- **Schema antigo em volume existente:** init scripts nao sao migrations.
  Aplicar ALTER versionado; para laboratorio descartavel, uma recriacao explicita
  dos volumes elimina **todos** os dados. Nao executar isso em dados reais.
- **Restore NuGet:** configuracao do repo usa nuget.org, sem alterar a config global.
- **Verify timeout de projecao:** olhar logs API, broker e outbox; nao aumentar
  timeout para mascarar mensagem permanentemente invalida.
- **Rodar load e verify juntos:** pode quebrar as assercoes globais de convergencia.
