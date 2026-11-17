# Fase 6 - observabilidade

## Desenho aprovado e responsabilidades

O usuario aprovou a Fase 6 apos a entrega da confiabilidade. O bloqueio
Docker/WSL das integracoes anteriores permanece; nao foi contornado por
servicos falsos nem por reinicio automatico da maquina.

- `telemetry.py` concentra logs JSON, spans, metricas e bootstrap OTel. Usa
  providers SDK de propriedade do host, sem substituir os providers globais
  de uma aplicacao que incorporar o pacote.
- Runtime, modelos, ferramentas, worker e armazenamento publicam sinais
  nos pontos reais de execucao, preservando retries e checkpoints existentes.
- `audit.py` projeta apenas metadados tipados dos ledgers e fornece o
  snapshot de metricas por execucao. Contabilidade duravel usa `Decimal`.
- A migracao 2 adiciona `execution_audit`. Cada escrita de negocio e seu
  evento sao confirmados ou revertidos juntos, sem depender do coletor.

Trade-off principal: tracing e metricas sao diagnosticos best-effort;
auditoria e estado PostgreSQL sao a fonte duravel. Nao ha garantia de
exactly-once no exportador nem substituicao da auditoria por logs.

## Tracing e correlacao

Spans implementados:

| Span | Fronteira |
|---|---|
| `harness.admission` | Admissao de execucao ou agendamento de workflow |
| `harness.worker` | Segmento de processamento/retomada, incluindo contexto |
| `harness.execution` | Execucao do runtime, incluindo checkpoint terminal |
| `harness.context` / `harness.step` | Sumarizacao ou passo logico com retries/fallback |
| `harness.model` | Tentativa fisica do provedor, incluindo timeout/cancelamento |
| `harness.tool_invocation` | Invocacao logica da ferramenta |
| `harness.tool` | Callback fisico de ferramenta |
| `harness.checkpoint` | Checkpoint e auditoria atomicos |
| `harness.cancel` | Solicitacao persistida de cancelamento |

`execution_id` e os IDs de tentativa/invocacao correlacionam spans, logs e
auditoria. Os IDs nao se tornam labels de metricas. `Execution.traceparent`
persiste somente o contexto W3C; nao persiste baggage, headers nem tokens.
Workers abrem novo segmento com link para o contexto de admissao ou da
tentativa anterior. Workflows preservam contexto para ligar os filhos.
Sem provider de tracing ativo nao e inventado um trace ID.

Excecoes nao geram eventos automaticos com mensagem/stack. O span recebe
status e categoria de falha sanitizados. Timeouts de modelo sao classificados
como timeout, nao como cancelamento solicitado pelo usuario. Erros de
checkpoint propagam normalmente e marcam tambem o span da execucao.

## Logs seguros

As CLIs configuram logs JSON em stderr apenas para os namespaces
`agent_harness` e `opentelemetry`, restaurando handlers anteriores ao sair.
Stdout continua reservado ao resultado/comando.

O formatter usa allowlist de eventos e campos. Descarta mensagens livres,
argumentos de formatacao, extras desconhecidos e tracebacks, em vez de
depender exclusivamente de regex de segredos. Logs desconhecidos aparecem
como `redacted_log`; erros de exportacao como `telemetry_export_failed`.
Inclui timestamp UTC, nivel, evento, operacao/categoria quando permitidas,
execution ID e trace/span IDs quando presentes.

Nao sao exportados prompts, respostas, argumentos/resultados de ferramentas,
memoria, subject, DSNs, credenciais, headers ou mensagens de erro externas.
Nomes de modelo/ferramenta sao hashes em spans; provedor e limitado a
`openai`, `anthropic` ou `custom`. Nomes/configuracoes arbitrarios nao
expandem labels nem entram como texto livre.

A politica vale para os handlers configurados pelo harness; uma aplicacao
que instalar outros handlers/instrumentacoes precisa aplicar politica
equivalente. Nao ha auto-instrumentacao HTTP que capture payloads.
Checkpoint e historico de conversa continuam contendo os dados necessarios
ao runtime, sob as regras da Fase 4; redacao de telemetria nao os apaga.

## Metricas

| Instrumento | Medicao |
|---|---|
| `harness.operations` | Operacoes por tipo e categoria de falha |
| `harness.operation.duration` | Latencia em segundos nessas mesmas dimensoes |
| `harness.model.tokens` | Tokens conhecidos por provedor/direcao |
| `harness.model.estimated_cost` | USD estimados de tentativas com usage conhecida |
| `harness.model.unknown_usage` | Tentativas sem consumo reconciliado |
| `harness.queue.depth` | Gauge de jobs queued/running no escopo do worker |
| `harness.queue.age` | Tempo desde admissao ate claim, distinguindo recuperacao |
| `harness.lease.recoveries` | Claims com fencing token maior que 1 |
| `harness.circuit.decisions` | Decisoes granted/blocked/probe por implementacao local/Redis |

Metricas de fila sao atualizadas a cada dez segundos por worker; a leitura
respeita o escopo de tenant configurado, sem adicionar tenant como label.
Cada processo tem `service.instance.id` aleatorio no Resource OTel.
Dashboards devem respeitar esse escopo: nao somar snapshots identicos de
workers da mesma fila. Em recuperacao, queue age mede idade desde criacao,
nao exclusivamente a ultima espera.

Nao existem IDs de execucao, usuario, sessao, tenant, ferramenta, modelo,
texto de erro ou credential hashes como labels. Categorias de falha sao
um conjunto fechado. Tokens/custos de erro conhecidos tambem entram nos
instrumentos; chamadas sem usage geram medicao de incerteza, nao custo zero
apresentado como completo.

Exportacao e reinicio podem perder ou repetir diagnosticos. Para valores
por execucao use o snapshot persistido via `metrics`; nao some novamente
totais de cada checkpoint nem use contadores OTel como fatura. A precificacao
permanece a da Fase 5, incluindo as limitacoes de cache e consumo desconhecido.

## Auditoria transacional

Eventos cobertos:

- Admissao, claim, recuperacao e pedido de cancelamento (inclusive workflow).
- Todos os checkpoints, com versao, fencing token, status e totais.
- Inicio/conclusao/falha/interrupcao/bloqueio de cada tentativa LLM.
- Preparacao, inicio, sucesso, rejeicao, falha, replay e resultado incerto
  de invocacoes, mais registros individuais de cada tentativa de ferramenta.

Eventos registram timestamps, IDs, estados, categorias, consumo/custo quando
conhecidos e hashes de recursos; nao copiam conteudo privado. O ator da
auditoria e a identidade do principal, worker ou workflow. `subject` existe
**na auditoria autorizada**, nao nos logs/metricas/traces.

As chaves de evento sao estaveis e unicas por execucao; conflitos identicos
nao acrescentam duplicatas. Checkpoint, auditoria, finalizacao de job,
liberacao de sessao e historico terminal usam a mesma transacao e verificacao
de lease/versao. Se a auditoria falha, a atualizacao de estado tambem falha.
Auditoria de cancelamento e admissao usa a transacao da propria acao.
Nao se repetem efeitos externos para tentar melhorar logs.

O trigger rejeita UPDATE, DELETE e TRUNCATE. Isso e append-only na aplicacao,
nao WORM criptografico: administrador/superuser capaz de alterar schema pode
desabilitar o trigger. Retencao, backups e permissoes operacionais precisam
ser definidos antes de producao. Heartbeats nao geram um evento por tick:
claim/recuperacao/fencing e checkpoint registram a posse relevante.

Execucoes anteriores a migracao recebem baseline do estado conhecido ao
primeiro claim. Baseline nao inventa eventos passados nem recupera dados que
nunca foram persistidos. Falhas anteriores a admissao/autorizacao nao sao
gravadas como execucao pertencente a outro tenant.

Leitura exige tenant + dono do recurso e usa cursor de sequencia com limite
1..500. Essa e a fronteira confiavel existente, nao OIDC/RBAC/RLS completo.
O endpoint HTTP publico continua reservado a Fase 7.

## Operacao

1. Pare workers antigos e aplique `agent-harness-durable migrate`.
2. Inicie os novos workers. Migracao 2 e aditiva; nao ha downgrade automatico.
3. Para telemetria local com Docker funcional:

```powershell
# Configure previamente as senhas PostgreSQL/Redis exigidas pelo Compose.
& 'C:\Program Files\Docker\Docker\resources\bin\docker.exe' compose --profile observability up -d
$env:HARNESS_OTLP_ENDPOINT = 'http://127.0.0.1:54318'
& .\.venv\Scripts\agent-harness-durable.exe --config .\host.example.json worker
```

O Collector usa `otel-collector.yaml`, recebe OTLP/HTTP e imprime sinais no
exportador debug. A porta fica restrita ao loopback. E uma configuracao
local, sem backend consultavel, TLS ou autenticacao de producao.
Para um backend real configure TLS/auth e retencao no Collector; nao exponha
este receiver debug na rede. Headers de autenticacao do exportador podem ser
fornecidos via variaveis OTel padrao, nunca por codigo ou pelo exemplo JSON.

`HARNESS_OTLP_ENDPOINT` e opt-in. HTTP so e permitido em loopback; endpoints
remotos precisam HTTPS e nao podem conter senha, usuario, query ou fragmento.
O harness acrescenta `/v1/traces` e `/v1/metrics` a URL base.
Sem essa variavel, logs JSON continuam ativos e um host incorporador pode
fornecer providers OTel proprios; as CLIs nao iniciam exportador OTLP.

Spans usam fila limitada a 2048 e batches de 256; metricas saem a cada dez
segundos. Exportadores possuem timeout de tres segundos. Shutdown descarrega
as filas de forma best-effort; indisponibilidade do Collector nao altera um
resultado ja confirmado no banco. SDK/exportador sao donos dos retries de
telemetria, nao das chamadas LLM.

Consultas locais autorizadas:

```powershell
& .\.venv\Scripts\agent-harness-durable.exe --config .\host.example.json metrics <execution-uuid>
& .\.venv\Scripts\agent-harness-durable.exe --config .\host.example.json audit <execution-uuid> --after 0 --limit 100
```

`metrics` retorna JSON com totais, status, incerteza e traceparent.
`audit` retorna uma pagina ordenada; use o ultimo `sequence` como proximo
cursor. Falta de acesso e erro, nao resultado vazio.

## Verificacao e limitacoes

Resultados locais:

- `pytest -q`: **261 testes passaram, 20 skipped** por dependerem dos servicos reais.
- `mypy`: **sem erros em 49 arquivos Python**.
- Parsing com gramatica Python 3.12: **49 arquivos validos**.
- `pip check`: **nenhum requisito quebrado**.
- Compose com profile observability: **configuracao offline valida**.
- Script PowerShell, exemplo JSON e help das duas CLIs: **validos**.
- Diagnosticos do editor nos modulos de telemetria, auditoria e PostgreSQL:
  **nenhum erro reportado**.

Testes locais usam SDK OTel real, exporters in-memory e servidor HTTP
loopback temporario que decodifica protobuf OTLP real. Verificam spans,
links, concorrencia, custos/tokens de erros, cardinalidade, logs hostis,
cancelamento, timeout, falha do exportador e comandos de leitura.
Esse servidor e um teste de contrato, nao e o Collector oficial.

Testes de integracao reais cobrem admissao/auditoria, rollback conjunto,
perda de lease, imutabilidade, isolamento e paginacao, alem do Collector
oficial recebendo spans e metricas. O runner `scripts\Test-Integration.ps1`
cria containers isolados, espera o receiver OTLP, executa a suite e limpa
somente seu proprio projeto. Sem URLs de teste, casos reais sao skipped.

O Docker Desktop continua sem engine Linux disponivel neste ambiente.
Integracoes PostgreSQL/Redis/Collector permanecem pendentes. Nao houve
chamada paga a LLM. Python 3.12 e suportado pelo contrato e parsing; a
execucao local continua em Python 3.14.5, nao em interpretador 3.12 real.
Nao ha backend de traces persistente, API publica, OIDC/RBAC/RLS, quotas
distribuidas monetarias ou aprovacao humana nesta fase.

A proxima fase e a 7: guardrails e seguranca. Nao foi iniciada.
