# Testes HTTP com k6

Execute a partir da raiz de `short-term-rental`. Os scripts usam apenas HTTP,
nao acessam PostgreSQL/RabbitMQ diretamente e nao alteram a implementacao da API.
Use somente um ambiente local/de teste autorizado.

## Cenarios

| Script | O que exercita | Efeito nos dados |
|---|---|---|
| [load.js](load.js) | Catalogo, disponibilidade, escrita ou mistura | Reservas criadas ficam no banco |
| [race.js](race.js) | Mesmo imovel/datas, guests/chaves diferentes; um vencedor e demais 409 | Cancela as reservas vencedoras conhecidas em finally |
| [idempotency.js](idempotency.js) | Reserva inicial + replays concorrentes, payload diferente e replay depois de cancelar | Cancela a reserva inicial no teardown |

O teste de idempotencia exercita retries de uma reserva ja criada; nao e a
disputa concorrente por uma chave ainda inexistente. O verificador C# tambem
cobre esse ultimo caso e SQL direto, inbox, poison messages e convergencia:
[Verification.cs](../../src/ShortTermRental.Simulator/Verification.cs).

## Executar com k6 instalado

Instale o k6 seguindo a [documentacao oficial](https://grafana.com/docs/k6/latest/set-up/install-k6/).
Nao e necessario instalar pacotes npm. Exemplos PowerShell:

```powershell
# Leitura de metadados (cache Redis). Defaults: 10 VUs e 20s.
k6 run .\scripts\k6\load.js

# Disponibilidade: consulta o banco, nao o cache de metadados.
k6 run -e VUS=20 -e DURATION=20s -e READ_MODE=availability -e START_DATE=2200-02-01 .\scripts\k6\load.js

# Mistura aproximada: 90% tentativas de leitura e 10% de criacao.
k6 run -e VUS=20 -e READ_PERCENT=90 -e START_DATE=2200-03-01 .\scripts\k6\load.js

# Escrita sem hotspot intencional.
k6 run -e VUS=20 -e READ_PERCENT=0 -e START_DATE=2300-01-01 .\scripts\k6\load.js

# Disputa: defaults 32 tentativas em um unico batch.
k6 run -e START_DATE=2200-04-01 .\scripts\k6\race.js

# Replays: defaults 16 VUs e 64 iteracoes compartilhadas no total.
k6 run -e START_DATE=2200-05-01 .\scripts\k6\idempotency.js
```

As datas sao exemplos, nao garantia de disponibilidade. `START_DATE` e
obrigatoria para escrita, disponibilidade, disputa e idempotencia.
Escolha uma janela livre e nao execute testes que alteram as mesmas datas
simultaneamente.

## Sem instalar k6: Docker Desktop

O laboratorio foi validado com a imagem fixada `grafana/k6:1.3.0`.
O mount abaixo e somente leitura; o teste roda localmente e nao usa k6 Cloud.

```powershell
$mount = "type=bind,source=$((Get-Location).Path),target=/work,readonly"
docker run --rm --mount $mount grafana/k6:1.3.0 run -e BASE_URL=http://host.docker.internal:58080 /work/scripts/k6/load.js
docker run --rm --mount $mount grafana/k6:1.3.0 run -e BASE_URL=http://host.docker.internal:58080 -e START_DATE=2200-04-01 /work/scripts/k6/race.js
docker run --rm --mount $mount grafana/k6:1.3.0 run -e BASE_URL=http://host.docker.internal:58080 -e START_DATE=2200-05-01 /work/scripts/k6/idempotency.js
```

Dentro do container, `localhost` e o proprio container: use
`host.docker.internal` para as portas publicadas no Windows. Os caminhos
`/work/...` sao internos ao container Linux, nao caminhos do host Windows.
Os comandos nao iniciam nem param os servicos do sistema.

Para validar sintaxe/opcoes sem fazer requests:

```powershell
docker run --rm --mount $mount grafana/k6:1.3.0 inspect -e START_DATE=2200-04-01 /work/scripts/k6/race.js --execution-requirements
```

## Configuracao

| Variavel | Default | Uso |
|---|---|---|
| `BASE_URL` | `http://localhost:58080` | Origem da API |
| `SECOND_URL` | Ausente | Alternar requests de teste entre duas APIs do mesmo banco |
| `START_DATE` | Ausente | Inicio da janela; formato yyyy-MM-dd |
| `VUS` | 10 em load; 16 em idempotency | VUs, ate 256/128 respectivamente |
| `DURATION` | 20s | Duracao de load, sintaxe k6 |
| `READ_PERCENT` | 100 | Percentual probabilistico de leituras, 0..100 |
| `READ_MODE` | catalog | catalog ou availability |
| `CONTENDERS` | 32 | Requests no batch de race, 2..128 |
| `ITERATIONS` | 64 | Total de replays, no minimo VUS, ate 10000 |

Setup e cleanup usam `BASE_URL`; os requests de teste alternam se `SECOND_URL`
for informada. As APIs precisam compartilhar o mesmo banco. O script nao
descobre nem comprova essa configuracao de infraestrutura.

Exemplo entre instancias:

```powershell
k6 run -e SECOND_URL=http://localhost:58081 -e START_DATE=2200-06-01 .\scripts\k6\race.js
```

### Datas de carga sem overlaps intencionais

O setup le ate 1000 imoveis. A iteracao global do cenario seleciona o imovel
e avanca dois dias a cada volta completa no catalogo. Assim, uma unica execucao
local nao escolhe estadias sobrepostas para suas novas reservas.
Leituras tambem consomem numeros de iteracao; lacunas nas datas sao esperadas.

Isso nao coordena execucoes independentes/distribuidas e nao consulta o maior
checkout no banco como o simulador C#. Nao reutilize a janela de uma carga
anterior: reservas/historico permanecem. A janela precisa ficar livre tambem
nas datas seguintes usadas pela carga. Qualquer 409 na carga e inesperado
e falha o teste; nao ha hotspot intencional nesse script.

## Assercoes, metricas e limites

- Todos os checks precisam passar: threshold `checks: rate==1`.
- Falhas HTTP/transporte falham o teste: `http_req_failed: rate==0`.
- Load exige 200 nas leituras e 201 nas novas reservas; `unexpected_response`
  precisa ser zero. 409, 429 e 503 nao sao escondidos nem retentados.
- Race exige exatamente um `race_winners` e `CONTENDERS - 1` `race_conflicts`.
  409 e esperado somente nesse contexto (e no check de payload diferente).
- Idempotencia exige 200, mesmo booking ID, header de replay e estado correto.
- `booking_confirmations` conta criacoes bem-sucedidas; `read_successes` conta
  leituras bem-sucedidas. Nao confunda `http_reqs` com bookings por segundo.
- As tags `name` separam operacoes; requests de setup/cleanup tambem entram
  nas metricas HTTP agregadas. A taxa dos counters no resumo usa o tempo da
  execucao, incluindo suas fases; nao e diretamente comparavel ao simulador C#.

VUs de load sao closed-loop: cada VU espera seu request antes do proximo.
Nao representam uma taxa fixa de chegadas nem capacidade de 100M/10M RPS.
Race usa um VU coordenador com `http.batch`, permitindo varios requests em voo.
Isso nao garante que todos chegaram exatamente ao mesmo tempo no servidor,
nem demonstra sobreposicao temporal das transacoes no banco.

Nao ha threshold de latencia universal: a maquina, o estado do banco e o
ambiente afetam o resultado. k6 mostra percentis; estabeleca SLOs antes de
transformar um limite de latencia em criterio de aprovacao.

Cleanup nao remove historico/outbox/inbox, apenas cancela reservas conhecidas.
Crash, encerramento forcado ou resposta perdida podem impedir o cleanup ou
ocultar o ID de uma reserva comitada. Falhas de cleanup sao explicitas; nesses
casos, investigue o ambiente antes de repetir. Estes testes nao aguardam a
projecao convergir nem consultam overlaps globais: use o verificador C# para
as garantias end-to-end adicionais.

## Smoke tests realmente executados

Executados localmente na API do ambiente anterior ainda ativo, antes de subir
os containers com o novo nome. Os contratos HTTP sao os mesmos; estes
resultados nao comprovam startup do novo Compose nem capacidade maxima:

- Catalogo: 2 VUs por 2s, 919 leituras bem-sucedidas; nenhum check/falha HTTP.
- Disputa: 16 tentativas, 1 criacao e 15 conflitos; replay e liberacao das
  datas apos cancelamento passaram.
- Idempotencia: 8 VUs, 32 replays; mesmo ID, rejeicao de payload diferente,
  cancelamento e replay sem ressurreicao passaram.

Os modos de carga de escrita/mista e disponibilidade foram adicionados,
mas nao tiveram carga real executada nessa validacao.
