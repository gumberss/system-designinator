# Entidades e contratos

Contratos de projeto para implementacao incremental; este documento nao e
um OpenAPI gerado nem declara endpoints disponiveis.

Na Fase 3, os contratos executaveis estao em
[domain.py](../src/agent_harness/domain.py) e as implementacoes de ferramentas
em [tools.py](../src/agent_harness/tools.py). `ModelResponse` passa a conter
decisao ou chamadas nativas, e `Message` suporta o papel `tool`.
Registry resolve nomes imutaveis com versao registrada; substituicao por
outra versao usa novo nome. As assinaturas de repositorios, orquestracao e
API abaixo continuam sendo desenho futuro, nao funcionalidades existentes.

## 1. Entidades

Todos os recursos privados incluem `tenant_id`, identificador UUID, timestamps
UTC e versao para concorrencia otimista. Relacoes entre recursos incluem o
tenant nas constraints, nao apenas no filtro HTTP.

| Entidade | Conteudo principal / invariantes |
|---|---|
| Principal | subject OIDC, vinculos ativos, roles e grants do servidor |
| AgentVersion | nome, instrucoes, provider/model, fallback, ferramentas e politica; versoes imutaveis |
| ToolDefinition | implementation_key, versao, schemas, permissoes, timeout, modo de idempotencia e aprovacao |
| Session | dono, ACL, historico ordenado e bloqueio de execucao mutante |
| Message | sessao, sequencia, papel, conteudo protegido e proveniencia |
| ContextSummary | intervalo resumido, modelo, versao, consumo e proveniencia |
| MemoryItem | escopo, origem, conteudo protegido, ACL e expiracao |
| Execution | principal, sessao, snapshot, estado, budgets, deadline e resultado |
| Step | indice monotono, tipo, tentativa, referencias e uso contabilizado |
| ToolInvocation | argumentos protegidos, hash, resultado, estado e chave idempotente |
| Approval | invocation_id, hash, prazo, decisao, aprovador e motivo |
| Checkpoint | versao do schema, no/passo atual e referencias suficientes para retomada |
| WorkflowVersion | DAG validado, entradas/saidas e mapeamentos de handoff |
| WorkflowNodeRun | execucao pai, no, dependencias, checkpoint e resultado parcial |
| Job | execucao/no, available_at, lease_owner, lease_until e fencing_token |
| QuotaReservation | tenant, execucao, limites reservados e reconciliacao de consumo |
| UsageRecord | provider/model, tentativa, tokens, moeda e custo estimado Decimal |
| AuditEvent | ator, acao, recurso, decisao, timestamp e correlation_id; append-only |
| OutboxEvent | evento transacional, tentativa de publicacao e disponibilidade |
| Evaluation | dataset/versao, execucao, avaliador, notas e justificativa humana opcional |

Snapshots registram versoes de agente, ferramenta, workflow e politica.
Configuracao de conexao e credenciais sao referencias, nunca valores secretos.

## 2. Transicoes de execucao

| Origem | Destino | Condicao |
|---|---|---|
| queued | running | Job adquirido, quotas e prazo validos |
| queued / running / waiting_approval | cancel_requested | Principal autorizado solicita cancelamento |
| running | queued | Retry recuperavel agendado com checkpoint |
| running | waiting_approval | Intencao e aprovacao persistidas atomicamente |
| waiting_approval | queued | Decisao aprovada valida; job criado na mesma transacao |
| waiting_approval | failed | Rejeicao, com codigo approval_rejected |
| cancel_requested | cancelled | Nenhum novo efeito iniciado; estado da chamada em voo registrado |
| queued / running / waiting_approval | expired | Deadline total atingido |
| running | succeeded | Saida validada e checkpoint terminal confirmado |
| queued / running / waiting_approval | failed | Erro irrecuperavel ou budget esgotado |
| cancel_requested | failed | Efeito em voo indeterminado exige reconciliacao |

Em corrida entre conclusao e cancelamento, vence a primeira transacao terminal
valida. A API informa o estado confirmado, nao promete desfazer acoes concluidas.
Uma aprovacao duplicada identica retorna a decisao existente; decisao conflitante
retorna `409`. Worker perdido com lease expirado e retomado a partir do
checkpoint, nunca de uma copia em memoria.

`ToolInvocation` distingue `prepared`, `waiting_approval`, `running`,
`succeeded`, `failed` e `outcome_unknown`. Efeito desconhecido nunca e descrito
como cancelado com seguranca.

## 3. Portas de aplicacao

Assinaturas conceituais; tipos serao definidos no codigo nas fases indicadas.
Todas as operacoes I/O sao assincronas e recebem escopo de seguranca e deadline
quando aplicaveis. Contexto de seguranca e produzido por autenticacao, nao por
argumentos livres do modelo.

| Porta | Operacoes e contratos |
|---|---|
| ModelProvider | `generate(ModelRequest, CallContext) -> ModelResponse`; `capabilities(model) -> ModelCapabilities` |
| ToolRegistry | `resolve(implementation_key, version) -> Tool`; `list_installed() -> ToolDescriptor[]` |
| Tool | `invoke(ValidatedArguments, ToolCallContext) -> ToolResult`; sem acesso ao catalogo global de segredos |
| PolicyService | `authorize_action(SecurityContext, Action) -> PolicyDecision`; negar por padrao |
| ContextManager | `build(SessionScope, Budget) -> PreparedContext`; `summarize(MessageRange) -> ContextSummary` |
| ExecutionRepository | `create`, `load_scoped`, `transition(expected_version, fencing_token)`, `save_checkpoint` |
| UnitOfWork | agrupa estado, uso, quotas, auditoria e job/outbox em uma transacao |
| JobScheduler | `claim`, `heartbeat`, `complete`, `reschedule`; updates condicionados ao lease/token |
| ApprovalService | `request(PreparedInvocation)`, `decide(AuthorizedDecision)`; retomada transacional |
| QuotaService | `reserve`, `reconcile`, `release`; operacoes atomicas e idempotentes |
| MemoryRepository | `read_scoped`, `write_scoped`, `expire`; ACL explicita |
| AuditSink | `append(AuditEvent)` dentro da transacao autoritativa |
| Telemetry | spans, contadores e histogramas; exportacao nao substitui auditoria |

`ModelRequest` contem mensagens tipadas, descritores autorizados de ferramentas,
limite de resposta e schema opcional. `ModelResponse` contem mensagem,
`finish_reason`, chamadas com IDs, uso e provider request ID. Conteudo de
razoamento interno nao e solicitado nem armazenado.

Respostas com chamadas malformadas, IDs duplicados, ferramenta nao oferecida
ou saida estruturada invalida sao erros classificados; nao se extrai JSON
arbitrariamente de texto para simular validacao bem-sucedida.

Erros tipados: `AuthenticationError`, `AuthorizationError`,
`ValidationError`, `QuotaExceeded`, `ContextBudgetExceeded`,
`ProviderRateLimited`, `ProviderTimeout`, `ProviderUnavailable`,
`ToolTimeout`, `ToolOutcomeUnknown`, `DependencyUnavailable`,
`ApprovalExpired`, `Conflict`, `Cancelled` e `DeadlineExceeded`.
Cada erro define codigo publico, possibilidade de retry e categoria de falha.

## 4. Persistencia e indices

- Tabelas correspondem as entidades; migracoes versionadas serao adicionadas
  na Fase 4. Uso de ORM ou SQL sera escolhido junto com essas migracoes.
- Unicidade: `(tenant_id, agent_id, version)`, `(tenant_id, session_id, seq)`,
  `(tenant_id, execution_id, step_index, attempt)` e uma decisao por aprovacao.
- `Idempotency-Key` e unica por tenant, principal e operacao, associada ao hash
  do request e recurso. Reuso com payload diferente retorna `409`; retencao
  documentada e configuravel.
- Chave de ferramenta e derivada de tenant, execucao e invocation_id, nunca
  somente de argumentos. Duas acoes intencionais iguais nao sao deduplicadas.
- Indices de jobs por estado/disponibilidade e de leases por expiracao.
  Listagens por tenant, criacao e ID usam cursor estavel.
- Auditoria e uso sao append-only para a role de aplicacao. Arquivamento e
  remocao por retencao exigem role operacional separada.
- Campos privados podem ser segregados do indice de auditoria para permitir
  retencao menor sem destruir metadados operacionais.

## 5. API versionada planejada

O contrato implementado esta no [guia de seguranca](phase-07-security.md#api-entregue)
e nos guias de [aprovacoes](phase-08-approvals.md#api) e
[avaliacao](phase-09-evaluation.md#avaliacao-humana-e-api).
A tabela abaixo registra o desenho-alvo. Campos adicionais nao sao prometidos
pelas respostas atuais. Os DTOs atuais estao em `/openapi.json`.

Autenticacao: `Authorization: Bearer <JWT>`. Nao existe tenant confiavel em
header livre ou corpo. Respostas e schemas serao gerados pelo FastAPI/Pydantic,
com propriedades extras proibidas em comandos.

| Metodo / rota | Entrada -> saida | Acesso / semantica |
|---|---|---|
| POST /v1/agents | AgentCreate -> AgentVersionRead | agent:write; 201 |
| GET /v1/agents | cursor/limit -> AgentPage | Recursos autorizados; 200 |
| GET /v1/agents/{id} | versao opcional -> AgentVersionRead | ACL; 200 |
| PATCH /v1/agents/{id} | AgentUpdate + If-Match -> AgentVersionRead | Nova versao; 412 se stale |
| DELETE /v1/agents/{id} | If-Match -> vazio | Desabilita novas execucoes; 204 |
| GET /v1/tool-implementations | cursor/limit -> ToolImplementationPage | tool:write; apenas instaladas |
| POST /v1/tools | ToolRegistration -> ToolRead | tool:write; 201 |
| GET /v1/tools | cursor/limit -> ToolPage | Apenas visiveis; 200 |
| GET /v1/tools/{id} | id -> ToolRead | ACL; 200 |
| PATCH /v1/tools/{id} | ToolUpdate + If-Match -> ToolRead | Versao/enable/grants; 412 se stale |
| DELETE /v1/tools/{id} | If-Match -> vazio | Revoga uso futuro e pendente; 204 |
| POST /v1/sessions | SessionCreate -> SessionRead | Sessao do chamador; 201 |
| POST /v1/executions | ExecutionCreate + Idempotency-Key -> ExecutionAccepted | execution:create; 202 |
| GET /v1/executions | cursor/limit/status -> ExecutionPage | execution:read + ACL; 200 |
| GET /v1/executions/{id} | id -> ExecutionRead | execution:read + ACL; 200 |
| GET /v1/executions/{id}/traces | cursor/limit -> ExecutionTracePage | audit:read; eventos sanitizados |
| GET /v1/executions/{id}/metrics | id -> ExecutionMetrics | execution:read + ACL |
| POST /v1/executions/{id}/cancel | CancelRequest -> ExecutionRead | execution:cancel + ACL; 202 ou 200 se terminal |
| GET /v1/approvals | cursor/limit/status -> ApprovalPage | approval:read; dono ou revisor com ACL |
| GET /v1/approvals/{id} | id -> ApprovalRequest + ETag | Mesmo escopo de leitura |
| POST /v1/approvals/{id}/decision | ApprovalDecision + If-Match -> ApprovalRequest | approval:decide + ACL; sem autoaprovacao; 200 |
| GET /v1/metrics | janela limitada -> TenantMetrics | Administracao do tenant; agregados isolados |
| POST /v1/executions/{id}/evaluations | HumanEvaluationCreate -> EvaluationRead | Avaliador autorizado; 201 |
| GET /v1/executions/{id}/evaluations | cursor/limit -> HumanEvaluationPage | Dono ou avaliador com grants |
| GET /v1/executions/{id}/evaluation-artifact | id -> ExecutionArtifact + ETag | Somente terminal; dono ou avaliador com grants |
| GET /v1/evaluation-rubrics | vazio -> rubrica versionada | evaluation:read |

API de traces expoe eventos operacionais, nao conteudo privado bruto nem acesso
irrestrito ao backend OTel. Agregados de tenant nao se confundem com endpoint
de scraping de infraestrutura, que sera privado e autenticado.

Schemas principais:

- `ExecutionCreate`: agent_id, agent_version opcional, session_id, input,
  output_schema opcional e limites solicitados. O servidor aplica o menor
  limite entre pedido e politica; input nao altera grants ou instrucoes base.
- `ExecutionAccepted`: execution_id, status, status_url e created_at.
- `ExecutionRead`: id, agent_version, session_id, status, timestamps,
  result opcional validado, error opcional, usage e pending_approval_ids.
- `ToolRegistration`: implementation_key instalada, version, politica e
  configuracao sem segredos; schemas devem corresponder a implementacao.
- `ApprovalDecision`: decision (`approve` ou `reject`) e reason nao vazio.
  A versao esperada e o header `If-Match`, nao um campo do corpo.
  Identidade e horario sao definidos pelo servidor.
- `ApprovalRequest`: identificadores da execucao/agente/invocacao, ferramenta
  e versao, argumentos JSON canonicos em **string**, hashes, prazo, status,
  versao e metadados de decisao. Argumentos sao dados nao confiaveis.
- `ExecutionMetrics`: passos, tentativas, duracoes, tokens informados/estimados,
  custo estimado, moeda e estado de reconciliacao.
- `HumanEvaluationCreate`: id UUID idempotente, artifact_sha256, rubric_version,
  scores inteiros 1-5 de correctness/relevance/safety e rationale limitado.
  Avaliador e data sao definidos pelo servidor; autoavaliacao e proibida.

Listagens possuem `items` e `next_cursor`, com limite maximo de pagina.
Erros seguem `application/problem+json`: type, title, status, code, detail
sanitizado, instance e correlation_id. Sem detalhes privados em `404` para
recursos fora do escopo; `403` para operacao proibida sobre recurso visivel.
`429` indica quota/rate limit; `503` dependencia indisponivel. Um erro do
provedor apos aceite aparece no recurso de execucao, nao em resposta HTTP
retroativa.
