# Fase 2 - Core runtime

Este documento registra a entrega historica da Fase 2. A
[Fase 3](phase-03-tools.md) estende o protocolo de modelos com chamadas
nativas de ferramentas e adiciona registry/executor opcionais, preservando
agentes sem ferramentas.

## Desenho aprovado antes da implementacao

O runtime e uma biblioteca assincrona, nao um servidor HTTP. Recebe um agente
versionado, um principal confiavel, a sessao e o input. Resolve a porta de
modelo por nome, monta mensagens isoladas e executa passos ate conclusao,
erro, cancelamento ou limite.

Escolhas e trade-offs:

- Um `Protocol` Python define `ModelProvider.generate`, evitando heranca
  obrigatoria e import de SDK no runtime.
- Contratos imutaveis Pydantic validam configuracao e decisoes; dataclasses
  representam mensagens e o estado transitorio. Nenhum ORM nesta fase.
- O adaptador usa httpx diretamente: nao precisamos de um SDK inteiro para
  um unico endpoint. O cliente HTTP pertence ao composition root/CLI.
- A decisao de passo usa JSON Schema estrito: `status` (`continue` ou
  `completed`) e `content`. Conteudo intermediario e progresso publico,
  nao cadeia de pensamento. Esse envelope e um contrato do harness, nao
  um protocolo nativo obrigatorio de todos os provedores.
- A API publica de execucao e `await Runtime.execute(...)`; nao ha servidor
  ou gerenciador de jobs embutido. A retomada duravel depende da Fase 4.
- Cada execucao recebe historico novo. Sessoes mutantes simultaneas no mesmo
  runtime sao rejeitadas, evitando mesclar contexto; nao e seguranca de sessao
  distribuida nem substitui ACL persistente.

## O que foi implementado

- Agente configuravel com versao, tenant, instrucoes, provider/model, precos e
  limites. O runtime copia o mapa de provedores recebido na inicializacao.
- Autorizacao local por tenant e allowlist de agentes do principal confiavel.
  Essa identidade deve ser construida por uma fronteira autenticada no futuro.
- Ciclo com estados `queued`, `running`, `succeeded`, `failed`, `cancelled`
  e `expired`, timestamps UTC, passos, resultado e falha tipada.
- Iteracoes limitadas, limite de bytes de input, timeout por chamada,
  deadline total e limite de resposta HTTP.
- Cancelamento por `asyncio.Event` interrompe a chamada em voo. Cancelar a
  task externa propaga `CancelledError` apos limpar tasks filhas e sessao.
- Sessoes distintas executam concorrentemente. Uma sessao ocupada retorna
  `SessionConflict`; nenhum lock fica retido apos erro ou cancelamento.
- Uso e custo estimado Decimal acumulados por chamada. Falha apos consumo
  conhecido preserva tokens; uso desconhecido e marcado
  `usage_complete=False`, nao apresentado como consumo zero confirmado.
- OpenAI Chat Completions com JSON Schema, selecao de modelo, Bearer secret,
  normalizacao de respostas e classificacao sanitizada de erros.
- Redirecionamentos HTTP nao sao seguidos. Ferramentas nao sao oferecidas;
  chamadas inesperadas de ferramenta sao rejeitadas.
- Truncamento por token limit, recusa e JSON invalido nao viram sucesso.
- CLI local com codigos de saida nao zero para falha e configuracao invalida.

## Execucao real, opcional e com custo

No PowerShell, apos a instalacao descrita no README:

```powershell
# Configure a chave por mecanismo seguro na sessao atual.
# Nao a salve em arquivos versionados ou no historico do terminal.

# Substitua pelos precos USD atuais do modelo escolhido.
# Os numeros abaixo sao apenas exemplo de configuracao, nao tabela oficial.
$env:MODEL_INPUT_USD_PER_MILLION = "1.00"
$env:MODEL_OUTPUT_USD_PER_MILLION = "4.00"

& .\.venv\Scripts\agent-harness.exe --model gpt-4.1-mini "Explique uma fila duravel em duas frases."
```

`OPENAI_API_KEY` precisa estar presente no ambiente. A CLI exige os dois
precos explicitamente para nao assumir custo gratuito. Estimativa usa tokens
informados pelo provedor e precos configurados, sem descontos de cache ou
cobranca reconciliada. Nao representa uma fatura.

O modelo precisa aceitar Chat Completions, `max_completion_tokens` e
Structured Outputs com o schema solicitado. Modelos incompativeis falham
explicitamente; nao ha downgrade silencioso.

A CLI cria tenant, agente e sessao locais efemeros para demonstracao; nao e
autenticacao multiusuario. Entrada na linha de comando pode aparecer na lista
de processos e no historico do shell: use somente dados nao sensiveis.
O cliente da CLI ignora proxies do ambiente (`trust_env=False`) e usa TLS
validado; uma implantacao corporativa deve configurar transporte aprovado
explicitamente, sem desabilitar verificacao de certificado.

## Testes e verificacao

Comandos de validacao:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
```

Cobertura funcional: multiplos passos, configuracao por agente, autorizacao
antes de chamar o modelo, bytes UTF-8, limites, deadline, cancelamento,
concorrencia, isolamento de mensagens, uso/custo, erros HTTP, transporte,
JSON invalido, schemas, recusa, truncamento, redirecionamentos e CLI.

O teste de integracao executa o runtime e o adaptador real juntos, com
transporte HTTP controlado por `httpx.MockTransport`, tanto para sucesso
quanto para saida final invalida. Nao comprova conectividade externa.

Ambiente observado: Python 3.14.5 no Windows. A verificacao de tipos usa
semantica Python 3.12; execucao em um interpretador 3.12 ainda nao foi
realizada. A versao minima permanece 3.12 e precisara de matriz CI.

Nenhuma chamada a API paga foi executada. Credenciais e disponibilidade real
da conta do usuario nao foram verificadas.

Resultados observados nesta entrega:

- `pytest -q`: **61 passed**.
- `mypy`: **Success: no issues found in 11 source files**.
- `pip check`: nenhuma dependencia inconsistente.
- CLI instalada: `--help` executado com sucesso.
- Parse AST com gramatica Python 3.12: 11 arquivos aprovados.
- Pylance: nenhuma falha de sintaxe no runtime e no adaptador OpenAI.

Esses resultados sao locais, nao uma certificacao de producao ou uma execucao
em todos os interpretadores suportados.

## Limitacoes e proximos passos

- Sem PostgreSQL, Redis, migracoes, checkpoint, recuperacao de crash ou
  historico persistente; encerrar o processo perde estado.
- Sem JWT/OIDC, API HTTP, RLS ou ACL de sessoes. Nao exponha a CLI como
  servico publico.
- Apenas um adaptador real nesta fase. Independencia e verificada pela
  porta e doubles; segundo provedor real e fallback entram na Fase 5.
- Sem registry de ferramentas, workflows multiagente, aprovacao humana,
  retries, breaker distribuido ou quotas monetarias.
- Sem sumarizacao ou contagem preventiva da janela de contexto. O numero
  de passos e payloads e limitado; contexto/token budget completo entra na
  Fase 4. Recusa de contexto pelo provedor e falha explicita.
- Sem OpenTelemetry, exportacao de metricas ou trilha de auditoria duravel.
  Timestamps, passos e totais retornados nao substituem esses recursos.
- Provedores precisam respeitar cancelamento cooperativo. Python nao pode
  terminar com seguranca uma coroutine que deliberadamente ignore cancelamento.
- Precos sao configurados por agente; nao ha reserva ou reconciliacao de
  custo desconhecido. Nao use como controle de gasto de producao.
- Ainda sem dependencias travadas e pipeline CI; hardening de deploy sera
  tratado antes de qualquer declaracao de prontidao de producao.

**Fase 3, sujeita a aprovacao:** registry de ferramentas, validacao de
argumentos/resultados, grants, politicas e contrato de idempotencia.
