# Fase 7: instrumentacao e medicao regional

Entregue `benchmark-osm`, admissao limitada, erros no denominador, percentis,
CPU/RSS de processos e guia de capacidade. **Validacao de escala global,
datasets medios/grandes e operacao HA permanece pendente.**
Esta fase nao transforma o laboratorio em um servico de producao.

## Executar

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
& .\.venv\Scripts\mapinator.exe benchmark-osm .\data\sao-paulo.osm --requests 20 --concurrency 2 --seed 42
& .\.venv\Scripts\mapinator.exe benchmark-osm .\data\sao-paulo.osm --requests 20 --concurrency 2 --source=-23.5596178,-46.6562147 --destination=-23.5538367,-46.6481601
# Instancia HTTP local dedicada; cria sessoes/rotas com TTL:
& .\.venv\Scripts\mapinator.exe benchmark-http --source=-23.5596178,-46.6562147 --destination=-23.5538367,-46.6481601 --requests 20 --concurrency 2
```

1–500 pedidos, 1–4 processos, somente ate `concurrency` jobs em voo.
Sem coordenadas fixas, seleciona pares de vertices de arestas reais de carro;
nem todos sao conectados. Caminhos inviaveis entram no denominador de
tentativas, mas nao no throughput/percentil de rotas bem-sucedidas.
CLI retorna JSON; nenhuma rota e substituida por linha reta.

`benchmark-http` mede autenticacao + POST de rotas privadas por loopback,
valida resposta de rota, admite ate quatro pedidos simultaneos, contabiliza
codigos de erro/status e latencia inclusive de falhas. Nao emite cookies,
tokens ou coordenadas. Smoke test TCP local: dois pedidos concorrentes
passaram contra a fixture explicitamente sintetica; isso **nao** comprova
throughput PostGIS real. Workload real HTTP continua pendente.

## Medicao realmente executada

Windows 11, Python 3.14.5, 12 CPUs logicas, host compartilhado.
OSM Sao Paulo, 2.802.200 bytes, 7.008 nodes, 3.211 arestas dirigidas, 7 segmentos.
SHA-256 `2e0cb8dc7e1af2c03ae371b4352799a81058fa97ce89163f919a1eaf044ba809`.
Origem/licenca em [manifesto](../data/sources.json).

| Workload | Pedidos/processos | Sucessos/erros | Duracao | p95 todas | Throughput de sucesso |
|---|---|---|---|---|---|
| Par real fixo, aproximadamente 1,82km | 8/2 | 8/0 | 1,111s | 0,769s | 7,203/s |
| Pares reais variados, seed 42 | 8/2 | 4/4 sem rota | 1,371s | 0,896s | 2,917/s |

CPU total dos workers: 0,375s e 0,453s; maior RSS amostrado por worker:
51.154.944 e 51.060.736 bytes, respectivamente. p95 de sucesso do workload
variado: 0,144s. Nao ocultar quatro tentativas inviaveis para melhorar o numero.

Sao runs muito curtos, apenas roteamento CPU offline. Incluem worker frio e
serializacao; grafo reconstruido por pedido. Nao incluem HTTP, PostGIS,
Kafka, Redis, WS, LLM, fila de usuarios ou rede. RSS amostrado a cada 10ms,
nao pico absoluto; medicao tambem tem overhead. CPU de tarefas pequenas pode
ser zero abaixo da granularidade do relogio, nao e medida substituida.
Nao inferir servidores por milhao de usuarios ou SLO dessa amostra.

## Instrumentacao e limites em operacao

`/metrics`: contadores HTTP por rota normalizada/status e duracao acumulada,
sem GPS, prompt ou IDs privados em labels. Health checks opcionais separados.
Transito expoe backlog, idade, DLQ, heartbeat/ultimo lag; navegacao tem CAS/TTL.
Roteamento/EV compartilham dois slots por instancia; saturacao retorna 429 e
timeout nao libera slot de trabalho que continua executando.
Interpreter local tem duas chamadas simultaneas, timeout e limite de resposta.

Teste opt-in de duas instancias ASGI usando SQL/Redis reais foi escrito,
mas nao executado por indisponibilidade dos endpoints. Docker Compose validou
configuracao, nao readiness real. Integrais nao sao aprovadas por mocks.

## Aceite ainda pendente para escala

1. Executar as integracoes reais e testes de restart/perda de dependencias.
2. Importar extratos reais medios/grandes com origem/checksum e limites
   explicitos; nao multiplicar grafo sintetico e chama-lo de dataset real.
3. Workloads HTTP/WS/ingestao mistos e prolongados, concorrencia, warm/cold,
   latencias cliente-servidor, erros, CPU/RAM/WAL e backpressure.
4. Referencia consentida de viagens reais para erro ETA/energia, nao zero
   ficticio quando ground truth esta ausente.
5. Particionamento/regioes, replicacao, TLS/OIDC, observabilidade distribuida
   e recovery antes de alegar HA ou disponibilidade 99,9%.

Dimensionamento futuro: instancias = ceil(pico_RPS / (throughput_medido
* utilizacao_alvo)), usando throughput do workload correto e p95 exigido.
O numero nao e preenchido enquanto essas medicoes nao existirem.
