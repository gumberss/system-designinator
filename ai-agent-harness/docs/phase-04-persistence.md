# Fase 4 - Contexto e persistencia

## Status e bloqueio atual

O usuario aprovou esta fase apos a Fase 3. O codigo foi implementado, mas
**a fase nao esta encerrada**: falta executar os dez testes de integracao
com PostgreSQL e Redis reais.

Neste computador, Docker Desktop nao disponibilizou o pipe do engine Linux.
O erro relatado pelo usuario foi timeout ao listar distribuicoes WSL.
Foi tentado iniciar o Docker Desktop e usar seu cliente atual, sem sucesso.
Nenhuma distribuicao WSL foi encerrada automaticamente.

Nao foram usados SQLite, fakeredis ou doubles como substitutos de uma
validacao real dos servicos. O script de integracao esta pronto para ser
executado assim que o engine funcionar.

## Desenho e trade-offs

- **PostgreSQL e a fonte de verdade.** Admissao, reserva de sessao e job
  compartilham transacao. Checkpoint terminal, conclusao de job e historico
  tambem sao confirmados atomicamente.
- **SQL direto com psycopg 3.** Esta etapa nao necessita ORM; migracao
  versionada e consultas explicitas tornam locks e fencing visiveis.
- **Conexoes curtas por operacao.** Nenhuma transacao permanece aberta durante
  uma chamada a modelo ou ferramenta. Pool e tuning dependem de medicoes.
- **Redis e sinal, nao fila autoritativa.** Sinais aceleram o worker, mas
  polling do PostgreSQL recupera trabalho mesmo se um sinal se perder.
  Falhas de Redis geram warning sanitizado e retorno explicito de entrega.
- **Checkpoints JSON versionados.** `Envelope` guarda snapshot de agente,
  principal confiavel, input e estado. Schema desconhecido falha fechado.
- **Orquestracao separada do runtime.** Um planner de DAG puro decide nos
  prontos; o adaptador transacional agenda seus jobs. O runtime continua
  executando um agente.
- **Sem LangGraph nesta fase.** O DAG limitado e aciclico nao exige outro
  scheduler. Adota-lo agora duplicaria fila/checkpoints e exigiria resolver
  consistencia entre duas fontes de estado. O contrato de modelo permanece
  independente dessa decisao.

Fontes principais:

- [Store PostgreSQL e leases](../src/agent_harness/adapters/postgres.py)
- [Migracao 1](../src/agent_harness/adapters/schema.sql)
- [Context manager](../src/agent_harness/context.py)
- [Worker](../src/agent_harness/worker.py)
- [Planner de workflows](../src/agent_harness/workflows.py)
- [Persistencia de workflows](../src/agent_harness/adapters/workflow_postgres.py)

## O que o caminho duravel implementa

### Estado e recuperacao

Sessoes sao identificadas por tenant e UUID e pertencem a um sujeito.
Admissao bloqueia a sessao e rejeita uma segunda execucao mutante com
`SessionConflict`. Chave idempotente repetida aceita somente o mesmo input,
agente e sessao, sem criar um segundo job.

Jobs usam `FOR UPDATE SKIP LOCKED`, lease com expiracao e fencing token
monotono. Cada escrita verifica propriedade, token, lease ainda valido e
versao otimista. Worker antigo nao pode confirmar um checkpoint depois
de perder a propriedade.

Antes de chamar um modelo, o runtime persiste a tentativa e consumo ainda
nao reconciliado. Resposta confirmada avanca o checkpoint; crash durante
I/O deixa uso desconhecido, sem transforma-lo em consumo zero na retomada.
Tentativas interrompidas contam no limite de chamadas de modelo.

Intencoes e UUIDs de ferramentas sao salvos antes do callback. Na retomada:

- Resultado ja confirmado pode ser reutilizado, com nova autorizacao.
- Intencao apenas preparada pode ser despachada.
- Leitura interrompida pode ser repetida.
- Efeito interrompido so pode ser repetido com suporte explicito a chave
  idempotente no adaptador e no destino.
- Efeito sem essa garantia termina como `tool_outcome_unknown`.

Essa e entrega at-least-once, nao exactly-once. Uma ferramenta que declare
idempotencia incorretamente continua sendo insegura; codigo instalado e
um limite de confianca do host.

### Contexto e memoria

Ao terminar, a execucao grava o turno de usuario e resposta final (ou falha
sanitizada) sem apagar o registro completo de passos no checkpoint.
Outra instancia de worker consegue carregar a sessao.

O context manager usa historico, resumo versionado e memoria da propria
sessao. Resumos e memorias entram como dados de usuario marcados como
nao confiaveis, nunca como instrucoes de sistema.

Se necessario, mensagens antigas sao resumidas por uma chamada real ao
provedor configurado, preservando o historico original. O resumo registra
intervalo de sequencias, versao, provedor e modelo. A consulta posterior
carrega apenas mensagens posteriores ao intervalo resumido.

Nao ha truncamento silencioso: contexto recente que nao cabe, uma mensagem
individual grande demais ou resumo invalido geram falha explicita.
O estimador usa bytes UTF-8, overhead e reserva de resposta. Ele e
conservador para os modelos usuais, mas **nao e um tokenizer universal
certificado**; consumo informado pelo provedor e guardado separadamente.
Schemas de ferramentas entram no calculo antes de cada chamada.

Memoria persistente tem chave, sessao, owner e TTL. Acesso usa tenant e dono,
ha limite de 64 itens ativos por sessao e expirados nao entram no contexto.
Nao existe memoria global compartilhada implicitamente entre agentes.

### Workflows

Definicao versionada com ate 32 nos, dependencias aciclicas, paralelismo
limitado, condicao declarativa de igualdade e handoff explicito.

- Sequencia: dependencias entre nos.
- Paralelo: nos prontos independentes, executados por workers concorrentes.
- Join: todos os predecessores obrigatorios precisam concluir.
- Condicao: resultado de predecessor comparado a literal, sem `eval`.
- Handoff: somente resultados dos predecessores nomeados sao copiados;
  contexto, memoria e grants nao sao herdados.

Cada no possui sessao e execucao proprias. O deadline absoluto do workflow
limita os nos, inclusive se todos os workers estiverem ocupados.
O reconciliador pode reiniciar e repetir um tick sem duplicar o job de um no.
Falha impede novas admissoes e solicita cancelamento de ramos em voo.
O workflow so fecha a falha depois de observar esses ramos terminarem;
efeitos concluidos nao sao desfeitos.

Os testes locais exercitam o planner. As garantias transacionais deste
desenho ainda precisam da suite real PostgreSQL descrita abaixo.

## Ambiente local

Reinstale as dependencias declaradas apos atualizar o projeto:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

O adaptador psycopg requer event loop selector no Windows. A nova CLI usa
essa factory explicitamente; os testes usam a hook de loop factory do
pytest-asyncio 1.5+.

Com o Docker Linux funcional, prepare credenciais locais aleatorias:

```powershell
$env:HARNESS_POSTGRES_PASSWORD = [guid]::NewGuid().ToString("N")
$env:HARNESS_REDIS_PASSWORD = [guid]::NewGuid().ToString("N")
docker compose up -d --wait
$env:HARNESS_DATABASE_URL = "postgresql://harness:$($env:HARNESS_POSTGRES_PASSWORD)@127.0.0.1:55483/harness"
$env:HARNESS_REDIS_URL = "redis://:$($env:HARNESS_REDIS_PASSWORD)@127.0.0.1:56483/0"
& .\.venv\Scripts\agent-harness-durable.exe migrate
```

Guarde credenciais fora do repositorio se quiser reutilizar esse volume
em outros terminais/reinicios. Gerar outra senha nao altera automaticamente
a senha de um cluster PostgreSQL ja inicializado.

O Compose publica somente loopback e tem healthchecks; nao constitui um
deploy de producao. Se o comando `docker` do PATH for antigo, use o cliente
em `C:\Program Files\Docker\Docker\resources\bin\docker.exe`.

### Submeter e executar

O [host de exemplo](../host.example.json) usa IDs sinteticos. Revise modelo
e precos antes de usar: valores de custo sao exemplos, nao precos oficiais.
O arquivo e configuracao confiavel do operador, nao identidade autenticada.
Nao coloque chaves, URLs secretas ou dados privados nele.

```powershell
$config = ".\host.example.json"
$session = & .\.venv\Scripts\agent-harness-durable.exe --config $config session
& .\.venv\Scripts\agent-harness-durable.exe --config $config submit `
  --agent 22222222-2222-4222-8222-222222222222 `
  --session $session --input "Some 20 e 22." --idempotency-key "local-request-1"
& .\.venv\Scripts\agent-harness-durable.exe --config $config worker --concurrency 2
```

O worker exige `OPENAI_API_KEY` no ambiente e chamadas reais podem custar.
Execute `status <execution-id>` ou `cancel <execution-id>` em outro terminal
com as mesmas variaveis e `--config`.

Outros comandos: `memory` para gravar memoria de sessao, `workflow <json>`
para submeter um `WorkflowDefinition`, e `workflow-status <uuid>`.
`--help` documenta os argumentos. Saida de status contem payload privado:
nao a encaminhe automaticamente a logs ou sistemas externos.

## Verificacao

Resultado local observado:

- pytest: **152 passed, 10 skipped**.
- mypy estrito: nenhum erro em 30 arquivos.
- Os dez skips sao testes de servicos reais sem URLs de teste configuradas.
- CLI duravel instalada: `--help` executado.
- `pip check`: nenhuma dependencia inconsistente.
- AST Python 3.12: 30 arquivos aprovados.
- Configuracao de host validada por Pydantic.
- PowerShell de integracao e Compose validados sintaticamente/offline.
- Pylance: nenhuma falha de sintaxe nos adaptadores PostgreSQL, worker
  e context manager verificados.

Nenhuma chamada paga a LLM foi executada. Doubles de provedores em testes
nao comprovam disponibilidade ou qualidade de modelo real.

Para a verificacao ainda pendente:

```powershell
& .\scripts\Test-Integration.ps1
```

O script cria um projeto Compose de nome unico, portas livres, senhas
temporarias, banco `harness_test` e Redis DB 15. Ao final remove somente
os containers e volume criados por esse projeto de teste e restaura as
variaveis anteriores. Nao usa `TRUNCATE` ou dados de outros projetos.

A suite cobre migracao, idempotencia, exclusao de sessao, isolamento,
leases concorrentes, fencing, reinicio de worker, recuperacao de ferramenta,
memoria, contexto, DAG e perda de sinal Redis. As URLs podem tambem ser
fornecidas como `HARNESS_TEST_DATABASE_URL` (banco terminado em `_test`) e
`HARNESS_TEST_REDIS_URL` (DB 15), sempre de uma instancia isolada.

## Limites e trabalho restante

- Integracao real bloqueada: nao declarar persistencia certificada ou fase
  concluida ate executar e corrigir essa suite.
- Sem OIDC/API HTTP/RLS, catalogo de agentes via API ou grants persistidos:
  o host e uma ferramenta administrativa confiavel. Hardening de acesso
  publico permanece na Fase 7.
- Sem retry/backoff/breaker, fallback ou reconciliacao automatica de custo
  desconhecido; Fase 5.
- Sem OTel e auditoria operacional completa; Fase 6. A tabela de eventos
  registra transicoes, mas nao substitui auditoria imutavel de producao.
- Sem aprovacao humana; ferramentas que a exigem continuam bloqueadas.
- Sem backup/restore testado, pool dimensionado, HA ou deploy de producao;
  Fase 10. Volumes locais so persistem no host.
- Encerrar o worker interrompe trabalho; lease/checkpoint sao o caminho de
  recuperacao. Ferramentas nao cooperativas continuam fora do suporte.
- Testes rodam em Python 3.14 no Windows; gramatica/tipos 3.12 nao substituem
  executar uma matriz de interpretadores em CI.

Proxima acao: reparar/iniciar o engine Docker e executar a suite real.
Posteriormente o usuario autorizou explicitamente a
[Fase 5](phase-05-reliability.md) apesar dessa pendencia. Essa autorizacao
nao substitui a verificacao real nem declara esta fase encerrada.
