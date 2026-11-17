# Fase 5 - confiabilidade

## Desenho e trade-offs

O usuario aprovou esta fase com PostgreSQL/Redis da Fase 4 ainda sem
validacao real devido ao Docker/WSL indisponivel. Essa pendencia permanece.
Nao houve chamadas pagas ou uso de credenciais reais para validar provedores.

- `ModelExecutor` centraliza retries, selecao/fallback, deadlines e ledger.
  Runtime e sumarizacao usam o mesmo caminho: nao existe retry oculto no
  adaptador HTTP que esconderia consumo ou contornaria checkpoints.
- `RetryPolicy` limita tentativas e usa exponential backoff com full jitter.
  Retry-After numerico ou HTTP-date e um minimo, nao e reduzido ao teto de
  jitter. Header invalido gera aviso sem registrar seu valor.
- `ModelCapabilities` exige declaracoes explicitas para fallback.
  Compatibilidade inclui ferramentas no historico, schema de decisao,
  contexto estimado, saida, residencia e tags de politica.
- `CircuitBreaker` separa decisao de resiliencia da coordenacao. O adaptador
  local atende uma unica event loop; o Redis usa Lua atomico e relogio do
  servidor, com geracao/token para rejeitar conclusoes de probes antigas.
- `AnthropicProvider` implementa Messages API real, ferramentas nativas e
  JSON-schema para decisoes finais, sem depender do SDK de outro provedor.
  O normalizador conserva texto publico de turnos com ferramentas e agrupa
  tool_results consecutivos em um unico turno de usuario.

Nao foi adicionada biblioteca generica de retry, ORM ou outro scheduler.
Transacoes PostgreSQL nao recebem retry automatico: falha na confirmacao
pode significar commit realizado, e repetir escrita cegamente e inseguro.

## Politicas realmente aplicadas

### Modelos

Somente `provider_rate_limited`, `provider_timeout` e `provider_unavailable`
recebem retry/fallback. HTTP 408, 429 e 5xx sao classificados como transientes;
autenticacao, rejeicao, recusa, schema e permissoes sao terminais. Fallback
nao tenta contornar recusa ou autenticacao.

Quando habilitada via `RetryPolicy()` ou o exemplo do host: tres tentativas
por modelo, base 0.5 s, teto de jitter 10 s e timeout de tentativa 10 s.
Agentes sem campo `retry` preservam uma tentativa e o timeout de chamada
anterior; a CLI efemera habilita a nova politica explicitamente. O conjunto
de tentativas, esperas e fallbacks
compartilha `call_timeout_seconds` (30 s por padrao). O prazo absoluto da
execucao continua valendo para fila, recuperacao, contexto e ferramentas.
Persistencia/atualizacao do breaker e limpeza cooperativa podem acrescentar
overhead ao prazo de uma chamada; nunca estendem o deadline da execucao.

`max_steps` limita interacoes logicas; `max_model_calls` limita chamadas
fisicas, incluindo retries, fallbacks e sumarizacoes. Ambos sao persistidos.
Fallback nao vem habilitado por padrao e nao reduz os limites de seguranca.
Rotas incompativeis ou com circuito aberto aparecem como `blocked` no ledger,
sem contar como chamada LLM. Primaria ausente/incompativel falha explicitamente.

Todas as rotas configuradas precisam de credenciais no worker. O host instala
apenas endpoints publicos globais OpenAI/Anthropic e rejeita residencia
regional declarada. Capacidades e tags sao declaracoes do operador confiavel,
nao certificacoes obtidas automaticamente do fornecedor. Hosts customizados
devem verificar suas proprias capacidades e contratos de dados.

### Ferramentas e efeitos externos

- Retry de ferramenta exige `side_effects=false` e configuracao explicita de
  `ToolDefinition.retry`; por padrao existe uma tentativa.
- Somente `tool_timeout` e `tool_unavailable` sao transientes.
- Ferramentas com efeitos nao podem configurar retry automatico, mesmo
  declarando idempotencia. A recuperacao duravel por chave externa permanece
  separada e sujeita as regras conservadoras da Fase 4.
- Cada dispatch revalida os grants; a entrada e validada antes do lote e
  a saida e validada em cada tentativa. O invocation ID permanece estavel.
- `max_tool_calls` limita chamadas logicas; `max_tool_attempts` limita
  dispatches fisicos, incluindo recuperacao e retries. `attempts` no ledger
  registra o numero de dispatches por invocacao.
- Resultado conhecido e salvo antes de atualizar o breaker. Falha de
  coordenacao posterior nao apaga sucesso, nao transforma o resultado em
  desconhecido e nao exige repetir o efeito.
- Timeout/cancelamento apos iniciar um efeito ainda produz
  `tool_outcome_unknown` quando o resultado nao e conhecido.

Callbacks devem ser assincronos cooperativos. Bloqueio da event loop por
codigo Python confiavel nao pode ser interrompido a forca por asyncio.
Uma chave idempotente nao fornece exactly-once sem suporte do destino.

### Breakers e indisponibilidade

Closed aceita chamadas; cinco falhas transientes consecutivas abrem o
circuito por 30 s. Retry-After pode abrir imediatamente pelo intervalo
informado. Apos a espera, somente uma probe half-open recebe permissao.
Sucesso fecha; falha reabre. A geracao impede que resposta antiga feche
circuito mais novo. Permissoes de probe expiram (310 s por padrao);
cancelamento tenta libera-las como neutras com limpeza limitada a 250 ms.
Falha de limpeza e avisada; a expiracao continua sendo a recuperacao segura.

No worker duravel, modelos e ferramentas compartilham Redis para coordenar
breakers entre processos. Chaves de modelo incluem escopo da credencial
por hash; ferramentas sao isoladas por tenant/nome/versao. Segredos nao sao
gravados nas chaves ou mensagens publicas. Rotacao de chave muda o escopo.
Workers que compartilham credenciais devem usar a mesma politica de breaker.
Ferramentas que acessam o mesmo backend podem declarar `dependency_key`
logica em comum; o tenant continua compondo a chave. Esse campo identifica
o grupo de dependencias/credencial, nunca deve conter o segredo em si.

Redis indisponivel nesse modo resulta em `coordination_unavailable`:
nao existe downgrade silencioso para um breaker local. Os sinais Redis
continuam opcionais para admissao/polling PostgreSQL, mas executar chamadas
protegidas pelo breaker distribuido exige Redis.

PostgreSQL e o proprio canal de sinais Redis usam breakers **locais**
independentes: proteger a conexao Redis usando o proprio Redis criaria uma
dependencia circular. Eles nao prometem coordenacao entre processos.
PostgreSQL tem connect timeout 5 s, statement timeout 5 s, lock timeout 3 s
e idle-in-transaction timeout 10 s. O timeout de statement pode ser alterado
no construtor do store. Sao limites por operacao, nao um timeout unico de
uma transacao composta. Escritas nao sao repetidas automaticamente.
Sinais Redis tem timeouts de conexao/leitura de 3 s e degradacao avisada.

O daemon volta a procurar jobs com backoff quando storage/coordenacao estao
indisponiveis e apos perda de lease. Outras falhas de implementacao nao sao
ocultadas. Lease/fencing e o checkpoint continuam sendo a fonte de recuperacao.

## Checkpoints, consumo e custos

Cada tentativa fisica cria `model_call_records` com modelo/precos usados,
finalidade (`step`/`summary`), timestamps, estado, request ID quando disponivel,
usage conhecida e custo estimado. O marcador de consumo desconhecido e salvo
**antes** do I/O. Falha no checkpoint impede chamar o modelo.

Falhas com usage contabilizam os tokens e o custo daquela rota. Falhas sem
usage e interrupcoes mantem `unaccounted_calls` e `usage_complete=false`,
mesmo quando retry ou fallback termina com sucesso. Recuperacao transforma
registros `running` abandonados em `interrupted`, sem apagar o consumo incerto.
Campos novos possuem defaults para ler envelopes da Fase 4 sem habilitar
retries implicitamente. A idempotencia reconhece o fingerprint anterior
somente quando as novas politicas sao equivalentes as antigas; alterar
politica continua gerando conflito. Contadores fisicos antigos sao
reconstruidos pelo minimo conhecido, nao como contagem historica exata.
O exemplo do host usa versao de agente 2 para explicitar a nova politica.
Essa compatibilidade e de leitura pela versao nova; workers antigos nao
devem consumir os novos checkpoints. Evite rollout misto ou downgrade sem
validar previamente os envelopes.

O custo da execucao soma tentativas conhecidas; o custo de um `Step` corresponde
apenas a resposta bem-sucedida daquele passo, nao as falhas anteriores.
Precos sao fornecidos pelo operador em USD por milhao. No Anthropic, contagem
de entrada inclui cache read/write, mas a estimativa usa a tarifa base
configurada: nao calcula descontos ou multiplicadores de cache. Nao e fatura,
reconciliacao automatica com o fornecedor ou quota monetaria distribuida.

## Configuracao

O exemplo `host.example.json` inclui a politica padrao. Para adicionar
fallback, declare `capabilities` na selecao primaria e `fallbacks` no agente:

```json
{
  "selection": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "input_usd_per_million": "1.00",
    "output_usd_per_million": "4.00",
    "capabilities": {
      "tools": true, "structured_output": true,
      "context_window_tokens": 32768, "max_output_tokens": 4096,
      "residency": "global", "policy_tags": ["operator-approved"]
    }
  },
  "fallbacks": [{
    "provider": "anthropic",
    "model": "claude-sonnet-4-5-20250929",
    "input_usd_per_million": "3.00",
    "output_usd_per_million": "15.00",
    "capabilities": {
      "tools": true, "structured_output": true,
      "context_window_tokens": 32768, "max_output_tokens": 4096,
      "residency": "global", "policy_tags": ["operator-approved"]
    }
  }]
}
```

O fragmento substitui esses campos de um agente completo; valores sao
**ilustrativos**, nao tabela oficial de precos ou descoberta de capacidades.
Confirme suporte ao schema/modelo e politicas antes de operar. Para esse host,
configure `OPENAI_API_KEY` e `ANTHROPIC_API_KEY` pelo ambiente; nao pelo JSON.

A CLI efemera tambem aceita `--provider anthropic --model <modelo>`.
Ela exige apenas a chave do provedor selecionado e as variaveis de preco
ja existentes, e continua sem durabilidade nem coordenacao distribuida:

```powershell
& .\.venv\Scripts\agent-harness.exe --provider anthropic --model claude-sonnet-4-5-20250929 "Resuma o que e um DAG."
```

O adaptador Anthropic usa JSON outputs `output_config.format`; remove
`minLength` do schema transmitido porque a API nao o suporta e mantem a
validacao original de `Decision` localmente. Referencia oficial:
[Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).

## Validacao e pendencias

Verificacao local final:

- `pytest -q`: **232 passaram, 13 skipped** (servicos reais indisponiveis).
- `mypy`: **sem erros em 42 arquivos Python**.
- Parsing com gramatica Python 3.12: **42 arquivos validos**.
- Configuracao exemplo: **valida, agente v2, retry explicito**.
- Help das duas CLIs: **validado**, incluindo selecao de provedor.

Testes locais cobrem backoff/jitter, Retry-After numerico/data, classificacao
HTTP, limites fisicos, custo/usage de falhas e fallback, contrato dos dois
provedores, historico de ferramentas entre adaptadores, cancelamento de I/O
e espera, probe half-open, concorrencia, geracoes obsoletas, revogacao de
grants, falha Redis e preservacao de resultado conhecido.

Os testes reais adicionais exercitam Lua atomico entre workers, expiracao de
probes e timeout de statement no PostgreSQL. Devem rodar junto aos testes da
Fase 4 usando `scripts\Test-Integration.ps1`; nunca sao substituidos por mocks.

Permanecem pendentes: integracoes reais PostgreSQL/Redis; chamadas reais aos
provedores; execucao em interpretador Python 3.12. O ambiente local usa
Python 3.14.5. Pylance nao respondeu nesta fase; pytest, mypy e parsing com
gramatica Python 3.12 fornecem a validacao local disponivel.

O usuario aprovou posteriormente a [Fase 6](phase-06-observability.md),
que implementa OpenTelemetry, logs estruturados, metricas e auditoria.
API publica/OIDC/RBAC/RLS/quotas atomicas continuam na Fase 7;
aprovacao humana e avaliacao nas fases seguintes. Nao ha alegacao de prontidao
para producao nem de eliminacao de prompt injection.
