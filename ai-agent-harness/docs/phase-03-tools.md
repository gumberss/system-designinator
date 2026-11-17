# Fase 3 - Ferramentas

## Desenho e trade-offs

Aprovada pelo usuario apos a entrega da Fase 2. O objetivo desta etapa e
permitir chamadas reais de ferramentas sem transformar texto do modelo em
autoridade ou codigo executavel.

Responsabilidades:

| Componente | Responsabilidade |
|---|---|
| `ToolRegistry` | Instalar implementacoes confiaveis, descobrir schemas, habilitar/desabilitar e administrar grants do tenant |
| `TypedTool[Input, Output]` | Conectar modelos Pydantic a callback assincrono tipado, validando entrada e saida |
| `ToolExecutor` | Preflight de lote, autorizacao, deadlines, deduplicacao, despacho sequencial e ledger |
| `ToolContext` | Entregar somente identidade/escopo, IDs, deadline e chave idempotente ao callback |
| Runtime | Oferecer apenas ferramentas autorizadas, limitar passos e encaminhar resultados como dados nao confiaveis |
| Adaptador OpenAI | Traduzir descritores, chamadas nativas e mensagens `tool`, sem executar ferramentas |

Fontes: [contratos](../src/agent_harness/domain.py),
[registry/executor](../src/agent_harness/tools.py),
[adaptador OpenAI](../src/agent_harness/adapters/openai.py) e
[calculadora](../src/agent_harness/adapters/calculator.py).

Decisoes:

- Nao foram adicionadas dependencias. Pydantic fornece schemas e validacao
  estrita; nao ha um segundo mecanismo de validacao JSON Schema.
- O catalogo pode receber novas implementacoes em tempo de execucao, mas
  somente pelo host confiavel. Nao existem uploads, imports, shell ou URLs
  arbitrarias vindos de uma API ou do modelo.
- Nomes nao podem ser sobrescritos. Uma nova versao deve usar outro nome,
  por exemplo `add_integers_v2`. Cada registro conserva a versao declarada.
- O executor processa lotes sequencialmente. Isso facilita interromper
  efeitos e conservar resultados parciais. Paralelismo multiagente pertence
  a orquestracao futura, nao e simulado por chamadas sem controle.
- O preflight valida permissoes e argumentos de todo o lote antes do primeiro
  efeito. As permissoes sao verificadas novamente antes de cada despacho.
- O ledger pertence ao objeto `Execution` e e **efemero**. Nao existe
  promessa de deduplicacao entre processos ou depois de crash.
- O adaptador mantem o schema estrito da resposta final. Para parametros de
  funcoes usa `strict=False` no protocolo OpenAI, permitindo defaults e
  schemas Pydantic sem conversao especifica. A validacao estrita efetiva e
  obrigatoria no servidor antes de qualquer callback.

## Fluxo implementado

1. O host registra callbacks e concede acesso ao tenant.
2. O principal confiavel e o agente precisam de allowlists proprias.
3. O runtime oferece somente a intersecao dessas tres permissoes.
4. O provedor retorna uma decisao final/progresso **ou** chamadas nativas.
   Misturar decisao e chamadas nao e um `ModelResponse` valido.
5. IDs, ferramentas oferecidas e limites sao verificados. Argumentos sao
   JSON object; duplicatas de chave, NaN, infinito, nesting excessivo,
   extras e tipos incorretos sao rejeitados.
6. O executor registra a intencao com ID, versao e hash canonico de argumentos.
7. Apenas execucoes `running`, no escopo correto, podem despachar chamadas.
   Uma execucao expirada nao pode iniciar um efeito.
8. O callback recebe argumentos tipados e `ToolContext`; resultado e
   revalidado, serializado e limitado em bytes.
9. A resposta volta como mensagem nativa `tool`, associada ao ID da chamada,
   com envelope `trust: untrusted_tool_data`. Nao vira mensagem `system`.
10. O proximo passo do modelo pode concluir. Se nao houver passo restante,
    o runtime nao inicia um efeito que ficaria sem possibilidade de resposta.

A calculadora incluida realiza soma inteira de verdade, com operandos
limitados e sem `eval`. Nao e um double de teste.

## Politica de acesso

Para despachar `nome`, todos os requisitos precisam ser verdadeiros:

- Tenant do principal, agente e execucao coincidem.
- Principal corresponde ao sujeito da execucao e pode executar o agente.
- `nome` consta de `Agent.allowed_tools`.
- `nome` consta de `Principal.allowed_tools`.
- Registry concede `nome` ao tenant e a ferramenta esta habilitada.
- A definicao corresponde ao descritor oferecido naquele passo.
- Ferramenta nao exige uma aprovacao ainda indisponivel nesta fase.

Negacao nao chama o callback. Revogar um grant tambem impede ler um resultado
deduplicado por meio de nova chamada. Revogacao nao desfaz uma operacao
externa que ja comecou.

Ferramentas `requires_approval=True` ficam ocultas da descoberta e recebem
`approval_required` se invocadas diretamente. Nao ha bypass ou aprovacao
automatica: pausa e retomada humanas entram na Fase 8.

## Extensibilidade

O host pode instalar a implementacao existente:

```python
from agent_harness.adapters.calculator import calculator_tool
from agent_harness.tools import ToolExecutor, ToolRegistry

registry = ToolRegistry()
registry.register(calculator_tool())
registry.set_tenant_grant(tenant_id, "add_integers_v1", True)
executor = ToolExecutor(registry)
```

`tenant_id` e um UUID obtido do contexto confiavel do host. Ao construir agente
e principal, inclua `"add_integers_v1"` em ambas as allowlists e injete
`tools=executor` no `Runtime`. O modelo nao pode fazer essas concessoes.

Para uma ferramenta nova, defina entrada e saida derivadas de `Contract`,
um callback `async (Input, ToolContext) -> Output` e uma `ToolDefinition`.
Registre um `TypedTool` com esses quatro componentes. Validadores devem ser
puros; I/O e efeitos pertencem ao callback. Erros operacionais devem ser
classificados como `HarnessError` com mensagem sanitizada pelo adaptador,
sem dados privados do destino. Bugs inesperados continuam propagando;
o ledger registra falha ou efeito desconhecido sem mascara-los como sucesso.

Schemas de entrada e saida devem proibir propriedades extras. `TypedTool`
rejeita configuracao raiz permissiva e revalida o resultado em vez de confiar
somente na anotacao de retorno ou em uma instancia criada sem validacao.

## Idempotencia e ledger local

- Identidade da chamada: objeto de execucao isolado + `call_id`. Argumentos
  normalizados (ordem de chaves ignorada) possuem hash SHA-256.
- Uma chamada concluida identica retorna o resultado registrado, sem novo
  callback. Acesso e reautorizado antes desse retorno.
- Mesmo ID com nome, versao ou argumentos diferentes e conflito.
- Mesmo ID ainda em andamento, rejeitado, falho ou indeterminado nao e
  despachado de novo automaticamente.
- Uma nova chamada intencional tem outro ID, mesmo se os argumentos forem
  iguais. Nunca deduplicar apenas pelo conteudo.
- Se a ferramenta declara `supports_idempotency=True`, o callback recebe
  chave derivada de tenant, execucao e invocation UUID. O adaptador externo
  precisa realmente encaminha-la e o destino precisa honra-la.
- Nao ha retry de ferramenta nesta fase, nem garantia de exactly-once.
- O loop do modelo rejeita IDs repetidos entre passos para manter a
  transcricao nativa consistente. Replay do executor e uma protecao
  independente, nao uma instrucao ao modelo para repetir IDs.

O registro inclui status, timestamps, latencia, resultado validado ou falha,
hash e replay count. Argumentos originais podem existir no historico privado
da execucao em memoria; nao sao enviados para logs pela implementacao.
Isso nao substitui auditoria duravel, protecao de dados em repouso ou politica
de retencao.

## Timeout, cancelamento e falhas parciais

O deadline de callback e o menor entre o prazo total da execucao e o timeout
da ferramenta. O helper assincrono usado por LLM e ferramentas cancela e
aguarda tasks filhas na saida.

Para ferramenta sem efeitos, timeout/falha encerra a chamada e cancelamento
e registrado como cancelamento. Para ferramenta com efeitos (o default
conservador), interromper uma chamada em voo ou receber um resultado
invalido pode significar que o efeito aconteceu:

- O registro fica `outcome_unknown`.
- O runtime termina com `tool_outcome_unknown`, inclusive quando a causa foi
  cancelamento ou deadline.
- A causa sanitizada continua no registro da chamada.
- Nao existe repeticao automatica ou promessa de rollback.

Cancelamento da task externa continua propagando `CancelledError`. O ledger
local e atualizado antes de propagar; sem persistencia ele ainda se perde
se o processo morrer.

Resultados anteriores de um lote sao preservados se um item posterior falhar.
Itens preparados ainda nao enviados sao marcados como nao despachados.
O resultado global nao e apresentado como sucesso parcial.

## Limites e seguranca

Configuraveis por agente: chamadas totais, chamadas por passo, bytes de
argumentos e de resultados. Esses limites complementam os limites de passos,
resposta de provedor e tempo da Fase 2.

Ferramentas sao codigo confiavel instalado, nao sandbox. Precisam ser
assincronas, nao bloquear o event loop e cooperar com cancelamento. Nao foi
implementada execucao isolada de plugins nao cooperativos ou codigo hostil.
Validacao de resultado e limite de bytes ocorrem depois que o callback
produz o valor: nao sao um limite de memoria para codigo arbitrario.

Prompt injection nao e eliminada. Um teste faz o modelo seguir texto
malicioso de uma ferramenta e verifica que a chamada privilegiada seguinte
e bloqueada, sem executar o callback proibido.

OIDC, API de administracao, ACL de sessao persistente, quotas distribuidas,
PostgreSQL/Redis e recuperacao de intencoes apos crash continuam ausentes.
Nao exponha este runtime como servico publico nesta etapa.

## Verificacao e encerramento

Comandos:

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
& .\.venv\Scripts\agent-harness.exe --help
& .\.venv\Scripts\python.exe -m pip check
```

Os testes incluem callbacks reais locais, protocolo OpenAI com transporte
controlado, CLI com calculadora ligada/desligada, intersecao de grants,
revogacao, schemas, limites, replays concorrentes, expiracao e efeitos
indeterminados. Os resultados finais medidos sao registrados abaixo.

Nenhuma chamada paga a um LLM ou sistema externo foi executada. HTTP real
da OpenAI foi substituido por `httpx.MockTransport` nos testes; isso valida
o protocolo construido, nao conectividade ou disponibilidade de um modelo.

Resultados locais desta entrega:

- `pytest -q`: **134 passed**, incluindo os 61 testes anteriores.
- `mypy`: **Success: no issues found in 18 source files**.
- CLI instalada: `--help` confirma a opcao `--enable-calculator`.
- `pip check`: nenhuma dependencia inconsistente; nenhuma dependencia nova.
- Parse AST com gramatica Python 3.12: 18 arquivos aprovados.
- Pylance: nenhuma falha de sintaxe nos arquivos de runtime, ferramentas e
  adaptador OpenAI verificados durante a implementacao.

Execucao de testes no ambiente Python 3.14 do workspace. Verificacao de tipos
e gramatica 3.12 nao substitui executar uma matriz de interpretadores em CI.

**Proxima fase, somente apos aprovacao:** contexto e persistencia, com
PostgreSQL, migracoes, jobs/leases, checkpoints, memoria isolada, resumo de
historico e desenho de workflows recuperaveis.
