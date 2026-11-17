# Entrega incremental e verificacao

## Protocolo de cada fase

1. Explicar desenho, responsabilidade dos componentes e trade-offs.
2. Implementar apenas o escopo aprovado, sem placeholders apresentados como
   recursos completos.
3. Executar testes direcionados e atualizar os documentos relacionados.
4. Apresentar implementacao, resultados reais, limitacoes e trabalho restante.
5. Aguardar aprovacao antes da proxima fase.

## Plano de fases e criterios de aceite

| Fase | Entrega prevista | Verificacao de aceite |
|---|---|---|
| 1 - Arquitetura | Fronteiras, diagramas, entidades, portas, estados, API e decisoes | Revisao documental; aprovacao do desenho |
| 2 - Runtime | Pacote Python 3.12+, ciclo limitado, estado tipado, selecao por agente, porta e primeiro adaptador LLM real | pytest: resposta final, multiplos passos, limites, cancelamento e concorrencia entre sessoes |
| 3 - Ferramentas | Registry de implementacoes confiaveis, schemas, grants, timeout e ledger de chamadas via portas | pytest: acesso negado antes da chamada, entrada/saida invalidas, idempotencia e efeitos desconhecidos |
| 4 - Contexto e persistencia | PostgreSQL, migracoes, jobs/leases, checkpoints, Redis, sessao, resumo, memoria, workflow sequencial/paralelo/condicional e handoff | Integracao real: reinicio de worker, fencing, contexto isolado, joins e perda de sinal Redis |
| 5 - Confiabilidade | Retry/jitter, breakers, deadlines propagados, reconciliacao, fallback e segundo provedor independente | Falhas injetadas, 429/Retry-After, timeout, half-open, cancelamento durante I/O e equivalencia de capacidades |
| 6 - Observabilidade | Logs estruturados, OTel, correlacao, metricas, uso/custo e auditoria transacional | Spans exportados para collector local, labels limitadas, redacao e contabilidade de tentativas |
| 7 - Guardrails e seguranca | FastAPI /v1 completa para recursos ja existentes, OIDC, RBAC/ACL, RLS, quotas atomicas, SSRF e saida estruturada | Testes negativos de auth, tenants/sessoes, revogacao, prompt injection, quotas concorrentes e inputs hostis |
| 8 - Aprovacao humana | Endpoints de aprovacao, pausa duravel, decisao assinada pela identidade e retomada | Reinicio durante espera, corrida approve/cancel, duplicatas, rejeicao, expiracao e revogacao apos aprovacao |
| 9 - Avaliacao | Datasets, regressao, rubricas, acuracia de tools, schemas e avaliacao humana | Runner reproduzivel com criterios abaixo e relatorio sem segredos |
| 10 - Prontidao | Dockerfile, Compose, health/readiness, carga, falhas, performance e runbooks | Deploy limpo, restauracao de backup, rotacao de chaves, carga sustentada e recuperacao documentada |

Controles locais de permissao e limites entram junto ao runtime/ferramentas,
nao sao adiados para tornar chamadas perigosas possiveis. A Fase 7 integra e
endurece a fronteira publica e o isolamento completo. Antes dela, testes usam
identidades explicitas; nao sera exposta uma API sem autenticacao como pronta
para producao.

A Fase 4 reavalia LangGraph com um workflow de referencia. Se adotado, sera
adaptador de orquestracao com checkpoints compatibilizados com a transacao da
plataforma, nao segunda fonte de verdade. A decisao e seus testes serao
documentados antes da implementacao.

## Estrategia de testes

### Unitarios e contratos

- Clock e fonte de jitter injetaveis para testar prazo e retry sem sleeps.
- Double deterministico de modelo somente em testes; adaptadores reais
  possuem testes de contrato com transporte controlado.
- Garantir que ferramenta proibida receba zero chamadas.
- Garantir que JSON invalido nao se torne resultado de sucesso.
- Garantir que logs e erros nao contenham JWT, segredos ou payloads privados.
- Testes de contrato compartilhados para todos os provedores.

### Integracao e falhas

- PostgreSQL e Redis reais em ambiente local isolado; sem substituir
  concorrencia do PostgreSQL por SQLite.
- Queda antes/depois de efeito externo; chamada indeterminada nao se repete.
- Lease expirado e worker antigo tentando gravar resultado.
- Crash entre aprovacao e retomada; outbox e job nao podem divergir.
- Redis indisponivel, provedor lento, timeout e falha de exportacao OTel.
- Corridas de admissao, quota, cancelamento, conclusao e decisao humana.
- Escritas e leituras cross-tenant usando a role real da aplicacao.
- Backup e restore com execucao pendente e aprovacao ainda nao decidida.

Integracoes externas ao vivo sao opt-in, com credenciais via ambiente, limite
de gasto e dados sinteticos. Testes com mocks nao serao descritos como
validacao de disponibilidade ou qualidade de um provedor real.

### Avaliacao e regressao

Dataset JSONL versionado com `case_id`, input sintetico, agente/versao,
ferramentas esperadas/proibidas, schema esperado e rubrica. Incluir:

- Resposta sem ferramentas e tarefa com varios passos.
- Escolha correta de ferramenta e argumentos.
- Conteudo hostil vindo de usuario, memoria e ferramenta.
- Saida estruturada, falha parcial e aprovacao/rejeicao.
- Casos negativos de acesso e isolamento de sessoes.

Gate deterministico: 100% de chamadas proibidas bloqueadas e de resultados
marcados como sucesso conformes ao schema; zero vazamento entre tenants nos
casos exercitados. Acuracia de tool usa precision/recall e correspondencia de
argumentos, nao apenas nome correto. Alvo inicial: F1 >= 0,95 no dataset de
referencia versionado. Qualidade com LLM real usa amostra, repeticoes, custos
e intervalo de confianca; thresholds e baseline sao aprovados na Fase 9.

Avaliacao humana registra identidade, rubrica, scores, justificativa e versao
do artefato avaliado; nao sobrescreve uma avaliacao automatica anterior.

## Operacao e performance planejadas

Compose incluira API, worker, PostgreSQL, Redis e collector OTel. Dados
persistentes usam volumes; credenciais sao injetadas e servicos de dados
nao ficam publicos por padrao. Migracoes rodam em job separado antes do
rollout; workers devem recusar schema de checkpoint incompativel.

Liveness verifica processo; readiness verifica dependencias necessarias para
aceitar trabalho com seguranca. SIGTERM para aquisicao de jobs, sinaliza
cancelamento e tenta checkpoint dentro de uma janela limitada. Trabalho
interrompido segue as regras de lease/idempotencia, nao some.

Carga tera perfil reproduzivel: hardware, versoes, numero de workers,
concorrencia, tamanhos de contexto, latencia simulada e duracao registrados.
Medir p50/p95/p99, throughput, fila, custo, memoria e conexoes. Separar teste
de overhead da plataforma, com transporte controlado, de latencia real de LLM.

Alvos preliminares para ratificar na Fase 10: aceite HTTP p95 < 300 ms com
20 clientes concorrentes por 10 minutos no ambiente de referencia, zero jobs
perdidos e recuperacao de job interrompido em ate duas janelas de lease.
Nao sao resultados medidos nem SLO contratual nesta fase.

Runbooks previstos: provedor degradado, Redis indisponivel, backlog crescente,
lease abandonado, custo nao reconciliado, efeito externo desconhecido,
aprovacao expirada, rotacao de segredos e recuperacao de desastre.

## Encerramento da Fase 1

**Entregue:** arquitetura, dois diagramas de componentes/fluxo e um diagrama
de sequencia, entidades, interfaces conceituais, API planejada, transicoes,
decisoes de seguranca/durabilidade e criterios das dez fases.

**Verificacao:** revisao documental de cobertura e consistencia. Testes
automatizados, carga, chamadas LLM e integracao: nao executados; nao existe
codigo executavel nesta fase.

**Limitacoes:** todas as capacidades operacionais dependem de implementacao.
Bibliotecas de persistencia e adaptadores concretos ainda serao selecionados
nas fases apropriadas. Diagramas estao em Mermaid e dependem de um renderizador
compativel para visualizacao grafica.

O usuario aprovou a Fase 2 apos a entrega documental. O encerramento acima
registra o estado historico da Fase 1.

## Fase 2

Implementacao e limites estao no [guia do runtime](phase-02-runtime.md).
O usuario aprovou a Fase 3 apos essa entrega.

## Fase 3

Registry, ferramentas tipadas, politicas e integracao nativa estao no
[guia de ferramentas](phase-03-tools.md). Ledger e deduplicacao permanecem
locais ao processo, sem alegacao de durabilidade.

O usuario aprovou a Fase 4 apos essa entrega.

## Fase 4 - validacao real bloqueada

Implementacao, operacao e evidencias estao no
[guia de contexto e persistencia](phase-04-persistence.md).
Os testes locais passam, mas PostgreSQL/Redis reais nao foram validados:
Docker Desktop esta bloqueado por timeout ao consultar as distribuicoes WSL.
O usuario recebeu instrucoes de reinicio manual; nenhuma distribuicao foi
encerrada automaticamente.

O usuario autorizou explicitamente a Fase 5 apesar desse bloqueio. A
implementacao prosseguiu sem declarar a Fase 4 validada.

## Fase 5 - confiabilidade

Politicas, adaptadores, configuracao e evidencias estao no
[guia de confiabilidade](phase-05-reliability.md).
Testes deterministas validam as falhas injetadas; as integracoes reais das
Fases 4/5 continuam pendentes de PostgreSQL/Redis funcionais.
O usuario aprovou a Fase 6 apos essa entrega, mantendo as pendencias reais.

## Fase 6 - observabilidade

Logs seguros, tracing, metricas e auditoria estao no
[guia de observabilidade](phase-06-observability.md).
OTLP/HTTP foi exercitado com um receptor local de contrato; isso nao substitui
Collector, PostgreSQL e Redis reais. As integracoes continuam bloqueadas
pelo Docker/WSL indisponivel. O usuario aprovou a Fase 7 depois dessa entrega.

## Fase 7 - guardrails, seguranca e API

JWT/JWKS, RBAC/ACL, API FastAPI, RLS forcada, logins separados, cotas
transacionais, rate limit Redis e validacao de resultados estao no
[guia de seguranca](phase-07-security.md). Resultado local: 332 testes
passaram e 26 integracoes reais foram skipped; mypy validou 59 arquivos.
O engine Docker Linux continua indisponivel: validacao real de RLS,
concorrencia de cotas, Redis e Collector permanece bloqueada. Essa pendencia
impede declarar prontidao de producao. Depois dessa entrega, o usuario
autorizou a Fase 8 mantendo a pendencia de validacao real.

## Fase 8 - aprovacao humana e retomada

Implementados politica por agente/ferramenta, vinculacao da acao exata,
pausa duravel, pedidos com RLS, decisao autenticada com If-Match, auditoria,
retomada fenced por worker e revalidacao de solicitante/revisor/ferramenta.
O usuario escolheu outro subject autorizado do mesmo tenant, sem autoaprovacao,
e espera contando no deadline original. Sessao e cota continuam reservadas.

Lotes aguardam todos os pedidos; rejeicao, cancelamento e expiracao acordam
o job sem autorizar efeitos. Consumo da aprovacao nao significa conclusao
do efeito externo; recuperacao nao repete efeitos incertos nao idempotentes.
Contratos, trade-offs, migracao 4, exemplo e limitacoes estao no
[guia de aprovacoes](phase-08-approvals.md).

Resultado local: **370 testes passaram, 35 skipped; mypy validou 64 arquivos**.
Gramatica Python 3.12, `pip check`, configuracao de exemplo, help das CLIs
e sintaxe PowerShell tambem passaram. Nenhuma chamada paga a LLM foi feita.
Os testes reais de concorrencia, RLS, cotas, worker e Collector continuam
sem execucao: engine Docker Linux indisponivel. Doubles nao encerram essa
pendencia nem comprovam atomicidade real. O usuario autorizou a Fase 9,
sem chamadas pagas e com avaliador independente.

## Fase 9 - avaliacao e regressao

Datasets JSONL versionados, scorer puro, F1 micro de nome/argumentos com
multiplicidade, schema estrito, gates de despacho/vazamento, intervalos
Wilson e comparacao contra baseline estao implementados. A CLI valida,
avalia artefatos, compara regressoes e executa via API gerenciada somente
com consentimento e reserva pessimista dentro do orcamento.

Avaliacao humana usa rubrica quality-v1, identidade autenticada independente,
hash do artefato/snapshot e registro append-only com auditoria e RLS.
A migracao 5 adiciona a role evaluator e a 16a tabela protegida. Intencao de
despacho e persistida antes do callback; evidencia legada incerta nao vira
falso sucesso. Hashes de agente sao estaveis entre processos distintos.

O usuario aprovou F1 >= 0,95, gates estritos de schema/seguranca e ausencia
de chamadas pagas nesta entrega. O baseline controlado exercitou 11 casos
no runtime real com provedor exclusivo de teste: F1=1,0, tres propostas
proibidas bloqueadas e nenhum vazamento de marcador. Isso nao certifica
qualidade de modelo nem substitui um baseline com LLM real.

Verificacao: **426 testes passaram, 36 skipped; mypy validou 72 arquivos**.
Gramatica Python 3.12 em 72 arquivos, datasets/perfis/configuracao, help
das CLIs, `pip check` e sintaxe PowerShell passaram. Testes rodaram em
Python 3.14.5. PostgreSQL/Redis/Collector reais seguem bloqueados pelo engine
Docker Linux indisponivel; nao declaramos locks/RLS/atomicidade comprovados.

Veja contratos, comandos, baseline, trade-offs e limitacoes no
[guia de avaliacao](phase-09-evaluation.md). A Fase 10 aguarda nova aprovacao.
