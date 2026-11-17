# Fase 9 - avaliacao e regressao

## Desenho e criterios aprovados

Avaliacao e separada da execucao: datasets descrevem expectativas; artefatos
representam execucoes terminadas; um scorer puro produz relatorios. O runner
real chama a API gerenciada existente, preservando autenticacao, grants,
cotas, checkpoints e aprovacoes. Nao instancia um provedor alternativo nem
executa ferramentas fora do runtime.

Escolhas confirmadas pelo usuario:

- F1 de ferramentas/argumentos >= 0,95.
- Nenhum despacho proibido e nenhum sucesso fora do schema nos casos
  exercitados; nenhum marcador de vazamento esperado pode aparecer na saida.
- Avaliacao humana por outra identidade autorizada do mesmo tenant.
- Nenhuma chamada paga nesta entrega; execucao real somente opt-in.

Nao foi adicionado LLM-as-judge: sem calibracao ele introduziria custo e uma
segunda fonte de erros. Correcao subjetiva usa a rubrica humana versionada.
Nao ha novas dependencias de runtime; a CLI reutiliza httpx, Pydantic e jsonschema.

## Datasets, artefatos e metricas

- `evaluations/reference-v1.jsonl`: 11 casos sinteticos de resposta simples,
  ferramentas/argumentos, multiplos passos, schema, entradas/memorias/resultados
  hostis, falha parcial, aprovacao/rejeicao e marcador de outra sessao.
- `evaluations/live-smoke-v1.jsonl`: tres casos sem injecao de falhas ou
  aprovacao automatica, adequados como ponto de partida para um ambiente real.
- `evaluations/agent-profiles-v1.json`: corpos de criacao dos tres agentes
  do smoke. Sao configuracoes de exemplo, nao agentes ja provisionados.
- `evaluations/reference-baseline-v1.json`: relatorio medido do **runtime real
  com provedor roteirizado exclusivamente de teste**, nao qualidade de LLM.
  Os campos provider/model identificam esse double explicitamente.

Cada caso fixa ID, versao do dataset, input, agente/versao, memorias opcionais,
expectativas, ferramentas esperadas/proibidas, schema, marcadores e rubrica.
JSONL rejeita chaves duplicadas, NaN, linhas vazias, IDs duplicados e limites
excessivos. Ate 100 casos, 10 repeticoes e 1000 amostras; arquivos ate 16 MiB,
linhas ate 1 MiB. Schema segue as restricoes de seguranca ja existentes.

O artefato inclui escopo, hash do input, agente/versao/hash, provider/model,
resultado, propostas de ferramentas com hashes dos argumentos, evidencia de
despacho, status, uso, custo estimado e latencia. A avaliacao exige amostras
completas, sem duplicatas, vinculadas ao input e alvo corretos.

Tool precision/recall/F1 sao **micro**, com correspondencia de nome e
argumentos JSON canonicos, incluindo multiplicidade. Nome certo com argumentos
errados conta FP e FN. Propostas recusadas antes do preflight tambem aparecem.
O estado final da execucao e avaliado separadamente: propor a ferramenta certa
nao transforma uma execucao falhada em sucesso.

Casos de ataque/injecao que deliberadamente simulam propostas hostis usam
`evaluate_tool_accuracy=false`. Eles exercitam o gate de seguranca, sem
contaminar a medicao de qualidade de escolha do modelo com um ataque injetado.
O relatorio informa `tool_scored_samples`; sem suporte, F1 e null, nao 100%.
`schema_support` informa quantos sucessos realmente tiveram schema avaliado.

Cada tentativa nova registra intencao de despacho em checkpoint antes do
callback. Isso acrescenta uma escrita duravel por tentativa, em troca de
evidencia conservadora: intencao **nao prova conclusao do efeito**. Metadados
legados insuficientes sao `unknown`, nunca presumidos bloqueados. Intencao ou
incerteza em ferramenta proibida falha o gate. Os contadores agregam
invocacoes logicas, nao todas as repeticoes fisicas de uma mesma invocacao.

Saida estruturada usa parsing estrito e validacao do schema esperado.
Vazamento usa marcadores sinteticos explicitos na resposta final: nao e um
detector universal de PII nem prova de ausencia de prompt injection.
O isolamento real entre tenants continua dependendo dos testes RLS reais.

O relatorio omite inputs, resultados, argumentos e justificativas, mas inclui
IDs, hashes, modelo, versao, scores, uso/custo e latencia p95 nearest-rank.
Hashes nao anonimizam valores de baixa entropia. Arquivos de artefatos **contem
resultados privados**: proteja-os e nao os envie para logs/controle de versao.

Intervalo de confianca de 95% da taxa de casos aprovados usa Wilson.
E descritivo, sob hipotese binomial; repeticoes correlacionadas nao provam
significancia estatistica nem qualidade universal do modelo.

## CLI e baseline reproduzivel sem rede

Depois de instalar `.[dev]`, a CLI e `agent-harness-eval`.
Todos os comandos abaixo partem da raiz deste projeto:

```powershell
& .\.venv\Scripts\agent-harness-eval.exe validate --dataset evaluations\reference-v1.jsonl

# Exportador SOMENTE de testes: runtime real + ScriptedProvider, sem rede/custo.
& .\.venv\Scripts\python.exe tests\test_evaluation.py --export-artifacts controlled-artifacts.jsonl
& .\.venv\Scripts\agent-harness-eval.exe score --dataset evaluations\reference-v1.jsonl --artifacts controlled-artifacts.jsonl --out candidate.json
& .\.venv\Scripts\agent-harness-eval.exe compare --baseline evaluations\reference-baseline-v1.json --candidate candidate.json
```

Saida 0: gates/comparacao passaram; 1: gates/regressao falharam; 2: erro de
entrada, dependencia ou operacao. Outputs existentes nunca sao sobrescritos.
A suite executa o dataset pelo runtime e o compara ao baseline versionado.

Comparacao exige mesmo benchmark, origem, criterios e numero de repeticoes.
Mudancas de input/expectativas/rubrica nao sao comparaveis silenciosamente.
Agente/versao podem mudar como alvo experimental; o relatorio registra os
alvos efetivos e hashes. `--agent-version 2` avalia uma versao nova sem alterar
expectativas; benchmark hash permanece, dataset hash muda.

Regressao inclui falhar gates, reduzir F1/taxa de acerto ou perder uma amostra
anteriormente aprovada. Latencia/custo sao reportados, mas ainda nao possuem
SLOs de performance: ficam para a Fase 10. Baseline nunca e promovido
automaticamente. Para altera-lo, gere outro arquivo, revise e versione
explicitamente. Relatorios nao sao atestacoes assinadas contra adulteracao.

## Runner real opt-in

Provisione os UUIDs de `agent-profiles-v1.json` na membership do solicitante,
registre `add_integers_v1` no tenant e crie os tres agentes via API usando
cada objeto desse arquivo. Ajuste `model_key` ao catalogo real antes da
criacao. Use API, PostgreSQL, Redis e worker seguro conforme as Fases 7/8.

Defina `HARNESS_EVAL_ACCESS_TOKEN` por canal seguro, com JWT real do solicitante;
nao passe tokens na linha de comando nem use credencial administrativa.

```powershell
& .\.venv\Scripts\agent-harness-eval.exe run --allow-live --base-url http://127.0.0.1:8000 --dataset evaluations\live-smoke-v1.jsonl --artifacts live-artifacts.jsonl --out live-report.json --repetitions 3 --max-cost-usd 1 --timeout-seconds 120
```

O runner aceita HTTPS ou HTTP loopback, sem credenciais/query na URL, sem
redirects e sem proxies herdados. Executa sequencialmente, com nova sessao por
amostra, limita chamadas/saida/contexto e pre-calcula reserva pessimista de
**todas** as amostras antes de iniciar execucoes. Se exceder o teto, nao admite
nenhuma. Precos dependem da configuracao: isso nao e garantia contratual da
fatura do provedor. Uso desconhecido ou gasto informado acima do teto interrompe
a avaliacao; artefatos parciais ja recebidos permanecem no arquivo.

Em timeout/falha solicita cancelamento quando conhece o ID da execucao.
Cancelamento e cooperativo e nao desfaz efeitos. Se a resposta de admissao
se perder antes de informar o ID, pode restar trabalho aceito: inspecione as
execucoes do solicitante e reconcilie antes de repetir. O runner nao promete
retomada transacional de um lote nem retry automatico de admissao ambigua.

O runner nunca aprova pedidos automaticamente. Casos `controlled_fixture`
exigem `--allow-controlled-fixtures` e host preparado para falhas, ferramentas
de teste e/ou revisor humano real. O smoke nao exige essas fixtures. Nao
instalamos uma ferramenta de pagamento fake nem habilitamos lookup de teste
no worker de producao. O baseline real deve ser estabelecido pelo operador
apos validar o ambiente; nao foi produzido nesta entrega.

## Avaliacao humana e API

A migracao **5** cria `human_evaluations` com RLS habilitada e forcada, FK de
execucao e protecao append-only; startup exige 16 tabelas protegidas.
Pare/atualize API e workers em conjunto, migre com login administrativo
separado e inicie as versoes novas. Workers antigos nao produzem a evidencia
nova de despacho; nao misture versoes para avaliar seus gates.

```powershell
& .\.venv\Scripts\agent-harness-admin.exe migrate
& .\.venv\Scripts\agent-harness-admin.exe member --tenant $tenant --subject 'avaliador-do-idp' --role evaluator --agent $agent --tool add_integers_v1
```

`member` substitui grants: informe todos os necessarios. `evaluator` le e
avalia agentes autorizados, mas nao cria execucoes nem aprova ferramentas.
Admin tambem avalia, **sem autoavaliacao**. Revisor precisa de grant do
agente e das ferramentas permitidas no snapshot. Dono pode ler seus
artefatos/feedback; outros usuarios precisam de role/grants de avaliador.
JWT autentica ator; body nao pode substituir tenant/identidade/horario.

| Rota | Contrato |
|---|---|
| GET `/v1/evaluation-rubrics` | Rubrica `quality-v1`, dimensoes e escala |
| GET `/v1/executions/{id}/evaluation-artifact` | Artefato terminal e ETag SHA-256 |
| POST `/v1/executions/{id}/evaluations` | Avaliacao humana imutavel; 201 |
| GET `/v1/executions/{id}/evaluations` | `items`, `next_cursor`, cursor UUID e limit 1-100 |

O POST recebe:

```json
{
  "id": "33333333-3333-4333-8333-333333333333",
  "artifact_sha256": "<hash do ETag, sem aspas>",
  "rubric_version": "quality-v1",
  "scores": {"correctness": 5, "relevance": 4, "safety": 5},
  "rationale": "Conferido contra a tarefa e a politica."
}
```

Scores sao inteiros estritos 1-5, com todas e somente as dimensoes da rubrica;
justificativa nao vazia, ate 1000 caracteres. Ator/horario vem do servidor.
Hash vincula exatamente input, snapshot, saida e evidencias avaliadas.
Execucao nao terminal ou hash divergente: 409; autoavaliacao: 403; recurso
fora do escopo: 404. Replay exato do ID retorna a avaliacao original;
alteracao usando o mesmo ID: 409. Novo ID cria nova avaliacao sem sobrescrever
a anterior nem qualquer relatorio automatico.

Insercao e evento de auditoria sao atomicos; auditoria nao grava justificativa
nem resultado. Feedback persistido continua sendo dado sensivel. Ainda nao
ha UI, quorum de avaliadores, adjudicacao ou rubricas editaveis por HTTP.

## Evidencias e limites

O baseline controlado passou os 11 casos com F1=1,0, nenhum despacho proibido
ou incerto e nenhum marcador vazado. Tres propostas proibidas foram
exercitadas/bloqueadas. Isso testa o **harness**, nao certifica um modelo.
Os custos daquele relatorio sao estimativas dos tokens do double, nao
despesas com provedor. Nenhuma chamada paga foi executada.

Ha testes de scoring, limiar exato 0,95, multiplicidade, schema, JSON hostil,
vinculacao do artefato, gates/regressao, CLI, budget, cancelamento e API JWT.
O teste PostgreSQL opt-in cobre avaliacoes concorrentes/idempotentes,
auditoria, append-only e RLS com logins restritos.

Verificacao local: **426 passed, 36 skipped** na suite completa; mypy
estrito sem erros em **72 arquivos**. Parser com gramatica Python 3.12
tambem passou nos 72 arquivos; execucao foi em Python 3.14.5, nao 3.12.
Datasets, perfis de agentes, configuracao, CLI instalada, `pip check` e
sintaxe PowerShell passaram. Um teste adicional entre processos com
PYTHONHASHSEED diferentes comprova hash estavel mesmo com tags em conjuntos.

PostgreSQL/Redis/Collector reais seguem sem validacao: o pipe
`dockerDesktopLinuxEngine` continua ausente. Doubles nao provam locks,
atomicidade ou RLS reais. Execute `scripts\Test-Integration.ps1` quando o
engine estiver funcional. A plataforma continua **sem prontidao de producao**.

A Fase 10 depende de nova aprovacao.
