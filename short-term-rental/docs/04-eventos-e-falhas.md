# 4. Eventos confiaveis sem vender "exactly once"

Codigo: [Messaging.cs](../src/ShortTermRental.Api/Messaging.cs).

## Por que nao publicar diretamente?

Dois recursos independentes (PostgreSQL e broker) nao participam de uma unica
transacao neste sistema:

```text
COMMIT reserva -> crash antes de publicar -> evento perdido
publicar -> rollback da reserva -> evento de reserva que nao existe
```

Outbox resolve o **dual write de intencao**: gravamos a reserva e o evento
no mesmo banco/transacao. Publicacao e posterior, recuperavel.

Para comparar esse fluxo HTTP com uma entrada de mensageria que permite
retry duravel, veja o
[doc 10: outbox versus idempotencia + republicacao](10-outbox-e-retry-duravel.md).
Ele explica quando podemos dispensar outbox, a ordem de commit/publicacao/ACK
e por que o consumidor de saida ainda precisa de idempotencia.

## Publisher: pelo menos uma vez

O worker abre transacao e seleciona ate 50 eventos pendentes:

```sql
SELECT event_id, payload::text
FROM outbox
WHERE published_at IS NULL
ORDER BY created_at
LIMIT 50 FOR UPDATE SKIP LOCKED;
```

- `FOR UPDATE` bloqueia o lote deste publisher.
- `SKIP LOCKED` deixa outra instancia pegar outro lote em vez de esperar.
- A mensagem e persistente, exchange/filas sao duraveis.
- `mandatory=true` exige que a publicacao seja roteavel.
- Publisher confirmations + tracking aguardam confirmacao do broker.
- Somente depois marcamos published_at e commitamos o lote.

Se um lote publicou mensagens, mas o COMMIT falhou, as mensagens serao
publicadas novamente. Isso e **esperado**. O `event_id` nao muda.
Confirms nao significam "consumidor executou".

O codigo atual executa um UPDATE por evento, mas so persiste as marcacoes
no commit do lote inteiro. Para discutir UPDATE em lote e a alternativa de
comitar somente os sucessos confirmados, mantendo falhas/timeouts pendentes,
veja o [doc 11: lotes e sucessos parciais](11-outbox-lotes-e-sucessos-parciais.md).

O laboratorio segura locks/conexao do PostgreSQL durante confirms de rede,
com timeout de 5s por mensagem. E simples para estudar, mas tem custo de
transacao longa. Producao poderia usar leases/claims, lotes ajustados ou CDC,
mantendo a recuperacao de claims e idempotencia.

## Consumidor: efeito transacional idempotente

1. Receber com `autoAck=false`, prefetch 16 e ate 8 callbacks concorrentes por instancia.
2. Validar envelope.
3. BEGIN no PostgreSQL.
4. INSERT na inbox com PK event_id. Em conflito, incrementar deliveries.
5. Somente se a primeira entrega, atualizar contagem da projecao.
6. COMMIT de inbox + contagem.
7. ACK no RabbitMQ.

Crash antes de commit: nao ha efeito persistido; reentrega tenta novamente.
Crash depois de commit e antes de ACK: evento volta; inbox impede contar de novo.
`deliveries` pode crescer; `processed_events` conta eventos unicos.

Cada callback usa sua propria conexao/transacao do pool. ACK usa
`multiple=false`: confirmar individualmente e essencial quando a ordem de
conclusao difere da ordem de entrega. Atualizacoes SQL da contagem sao atomicas.
Prefetch limita entregas em voo, nao garante capacidade ilimitada.

O default sequencial da biblioteca deixou o consumidor atrasado na primeira
carga local. O paralelismo limitado foi acrescentado depois de observar o
backlog; ainda exige medir banco/pool e ajustar admissao para nao sobrecarrega-los.

**A garantia e de efeito uma vez na projecao deste banco por event_id.**
Nao e exactly-once no transporte nem garantia para efeitos externos.
Para pagamento, use chave idempotente no provedor e reconciliacao.
Para email, uma inbox local nao resolve sozinha a janela entre envio e commit.

## Filas e poison messages

| Nome | Funcao |
|---|---|
| `booking-events` | exchange direct |
| `booking.changed` | routing key |
| `booking-statistics` | fila duravel da projecao |
| `booking-dead-letter` | exchange fanout de mensagens invalidas |
| `booking-statistics-dead-letter` | DLQ duravel para inspecao |

JSON/envelope invalido gera log de erro e NACK sem requeue: vai para DLQ.
Falha ao processar no banco fecha/recria o canal com backoff de 2s; mensagens
nao confirmadas voltam a ficar disponiveis.

Uma falha deterministica diferente de JSON (por exemplo, FK inexistente) pode
ficar em retry neste laboratorio. Producao exige classificacao de erros,
retry com limite/delay, DLQ para esgotamento e operacao de replay.
Nao descartar silenciosamente nem travar o consumidor em loop apertado.

O verificador republica um evento verdadeiro e comprova aumento de deliveries
sem aumento do efeito/numero de eventos unicos. Tambem publica `{}` e comprova
que a DLQ ganha uma mensagem. A mesma verificacao funciona com duas APIs.

## Matriz de falhas

| Falha | Reserva | Eventos/projecao | Recuperacao |
|---|---|---|---|
| Redis fora | continua no PostgreSQL | independente | warning + cache miss; risco de carga extra |
| RabbitMQ fora depois do startup | pode confirmar | outbox acumula, projecao atrasa | reconectar e drenar |
| Consumidor fora | pode confirmar | fila acumula | reentrega e inbox |
| PostgreSQL fora | nao confirmar | publisher/consumer tambem param | API 503; retries limitados |
| API morre antes de commit | rollback | sem evento comitado | retry mesma chave |
| API morre depois de commit | reserva existe | evento existe na outbox | retry recupera mesma reserva |
| Publisher morre apos confirm | reserva existe | mensagem pode duplicar | inbox |
| Disco/volume perdido | pode perder dados | fora da garantia local | backup/HA/recovery nao implementados |

Compose espera broker/Redis saudaveis no **startup**. Isso simplifica boot.
Durante runtime, banco e obrigatorio para confirmar; broker/cache sao
dependencias desacopladas. Nao confundir readiness do primary com saude total.

## Ordem de eventos

`ORDER BY created_at` no publisher nao garante ordem global entre publishers,
filas/redeliveries e consumidores. A projecao de contagem usa deltas comutativos,
por isso converge mesmo fora de ordem.

Uma projecao de estado ("confirmed", "cancelled", "refunded") nao deveria usar
cegamente "ultimo evento recebido vence". Adicione aggregateVersion,
roteamento por bookingId e regras para gaps/out-of-order.

## Operacao de producao que falta

Filas quorum em cluster dimensionado, limites de backlog, alertas de idade da
outbox e DLQ, poison retries limitados, contratos versionados, tracing,
retencao de outbox/inbox, reconciliacao e runbooks.

Neste Compose: broker unico, filas classic duraveis e volume unico.
Persistencia/confirm nao significam replicacao ou tolerancia a perda do node.

Referencias:
[RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms),
[concorrencia no cliente .NET](https://www.rabbitmq.com/client-libraries/dotnet-api-guide),
[dead-letter exchanges](https://www.rabbitmq.com/docs/dlx),
[PostgreSQL locking](https://www.postgresql.org/docs/17/explicit-locking.html).
