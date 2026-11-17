# AI Agent Harness

Fundacao arquitetural para uma plataforma de execucao de agentes de IA,
independente de provedores e preparada para evoluir em fases verificaveis.

## Estado atual

**Fase 9: avaliacao e regressao implementadas; integracoes reais ainda bloqueadas pelo Docker/WSL local.**

Inclui runtime assincrono de processo unico, adaptador real para OpenAI Chat
Completions e registry tipado de ferramentas com permissoes, timeouts e
ledger local. A Fase 4 adiciona adaptadores PostgreSQL/Redis, checkpoints,
contexto e workflows, mas os testes com servicos reais ainda precisam ser
executados. **Nao esta pronto para producao.**

A Fase 5 acrescenta retries limitados com jitter e Retry-After, breakers
locais/distribuidos, fallback explicito por capacidades, adaptador Anthropic
real e contabilidade de cada tentativa. O usuario autorizou esta fase mesmo
com a validacao real da Fase 4 pendente; isso nao encerra aquele bloqueio.

A Fase 6 adiciona logs JSON com allowlist, spans e metricas OpenTelemetry,
exportacao OTLP/HTTP opt-in, correlacao persistida e auditoria de execucao
append-only na mesma transacao dos checkpoints. As CLIs duraveis `audit` e
`metrics` usam o escopo do operador configurado. A auditoria nao grava
prompts, argumentos nem resultados de ferramentas.

A Fase 7 acrescenta FastAPI `/v1`, JWT RS256/JWKS fixo, RBAC e grants
servidor-side, RLS forcada com roles separadas, cotas pessimistas transacionais,
rate limit Redis e validacao de resultados por schema. API e worker gerenciado
nao usam a identidade livre das CLIs antigas. Provisionamento e operacao
estao no [guia de seguranca](docs/phase-07-security.md).

A Fase 8 adiciona pausa duravel antes de ferramentas protegidas, caixa de
aprovacoes autenticada e retomada por worker com revalidacao de permissoes.
A decisao exige outra identidade autorizada do mesmo tenant, vinculada aos
argumentos e a implementacao exatos. A espera conta no deadline original e
mantem sessao/cota reservadas. Veja o [guia de aprovacoes](docs/phase-08-approvals.md).
Isso nao encerra o bloqueio de validacao real.

A Fase 9 adiciona datasets JSONL, scoring de ferramentas/argumentos, gates
de schema e seguranca, baseline controlado versionado, comparacao de
regressoes, runner real opt-in com orcamento e avaliacoes humanas autenticadas.
O baseline usa provedor roteirizado somente nos testes: nao e evidencia de
qualidade de um LLM real. Veja o [guia de avaliacao](docs/phase-09-evaluation.md).
A Fase 10 ainda nao foi iniciada.

Stack definida: Python 3.12+, FastAPI, Pydantic, PostgreSQL, Redis,
OpenTelemetry, pytest e Docker Compose. LangGraph sera adotado somente se
trouxer beneficio demonstravel para os workflows; nao sera uma dependencia do
dominio.

## Documentacao

- [Arquitetura, diagramas e decisoes](docs/architecture.md)
- [Entidades, contratos e API](docs/contracts.md)
- [Fases, verificacao e operacao](docs/delivery.md)
- [Fase 2: desenho, uso, testes e limitacoes](docs/phase-02-runtime.md)
- [Fase 3: ferramentas, permissoes e idempotencia local](docs/phase-03-tools.md)
- [Fase 4: contexto, persistencia, workflows e bloqueio de validacao](docs/phase-04-persistence.md)
- [Fase 5: retries, breakers, fallback, provedores e limites](docs/phase-05-reliability.md)
- [Fase 6: telemetria, auditoria, operacao e evidencias](docs/phase-06-observability.md)
- [Fase 7: API, JWT, RBAC, RLS, cotas e operacao segura](docs/phase-07-security.md)
- [Fase 8: aprovacao humana, pausa duravel e retomada](docs/phase-08-approvals.md)
- [Fase 9: datasets, avaliacao, baseline e regressoes](docs/phase-09-evaluation.md)

## Como o desenvolvimento sera conduzido

Cada fase comeca com a explicacao do desenho e seus trade-offs. Ao termina-la,
serao apresentados o escopo realmente entregue, testes e resultados, limitacoes
e proximos passos. A fase seguinte depende de aprovacao explicita.

A Fase 2 implementa o ciclo basico de execucao, contratos de modelos e
selecao de modelo por agente. Doubles de provedor existem apenas nos testes.
O estado transitorio do runtime nao e um repositorio duravel; a coordenacao
local de sessoes nao e um lock distribuido. A Fase 3 adiciona chamadas nativas
de ferramentas, validacao, intersecao de grants e deduplicacao local.
Persistencia duravel, resiliencia distribuida e aprovacoes entram nas fases
correspondentes.

A Fase 4 oferece um caminho duravel separado da CLI efemera original.
Instalar os adaptadores nao transforma automaticamente a CLI original em
um servico persistente: use `agent-harness-durable` conforme o
[guia de persistencia](docs/phase-04-persistence.md).

## Instalar e validar no PowerShell

Dentro deste diretorio:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
& .\.venv\Scripts\agent-harness.exe --help
& .\.venv\Scripts\agent-harness-durable.exe --help
```

Python 3.12+ e necessario. Testes locais nao precisam de chave ou rede.
Os testes marcados `integration` exigem PostgreSQL e Redis reais e sao
reportados como skipped quando as URLs de teste nao estao configuradas.
Para execucao real e configuracao de precos, consulte o
[guia da Fase 2](docs/phase-02-runtime.md).

Com Docker Linux funcional, os testes de integracao usam containers isolados
e credenciais temporarias para PostgreSQL/Redis e Collector local, sem
alterar volumes de outros projetos:

```powershell
& .\scripts\Test-Integration.ps1
```

Para habilitar explicitamente a ferramenta local de soma (a chamada ao LLM
continua sendo real e pode gerar custo):

```powershell
& .\.venv\Scripts\agent-harness.exe --enable-calculator "Use a ferramenta para somar 20 e 22."
```

Sem essa flag a CLI nao instala nem concede a ferramenta. Ferramentas novas
sao implementacoes Python confiaveis registradas pelo host, nunca codigo
enviado pelo modelo ou pelo usuario.

## Limites de seguranca

O modelo nao e uma autoridade de seguranca. Ele pode propor chamadas, mas
somente o runtime pode autoriza-las. Resultados de ferramentas e memorias
recuperadas sao dados nao confiaveis. A arquitetura reduz o impacto de prompt
injection por isolamento, autorizacao e limites; nao promete eliminar o ataque
por meio de um prompt.

Nao adicione segredos, dados de clientes ou respostas privadas de provedores
aos exemplos, logs, datasets ou ao controle de versao.
