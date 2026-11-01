# 3. Concorrencia: quem fica com a reserva?

## O algoritmo errado

```text
Guest A: SELECT -> livre
Guest B: SELECT -> livre
Guest A: INSERT reserva
Guest B: INSERT reserva
```

Se o banco nao tem uma regra atomica, ambos vencem. Isso e TOCTOU: o resultado
da checagem pode ficar obsoleto antes do uso. Colocar esses passos em uma
transacao READ COMMITTED, **sem** constraint/lock adequado, nao conserta sozinho.

Uma leitura "available=true" e informacao de um snapshot, nao posse do quarto.
Somente o commit autoritativo estabelece a reserva.

## O fluxo implementado

Codigo: [BookingStore.cs](../src/ShortTermRental.Api/BookingStore.cs).

1. Validar IDs/datas e header UUID `Idempotency-Key`.
2. Abrir transacao PostgreSQL READ COMMITTED.
3. Tentar inserir `booking_requests` com `ON CONFLICT (idempotency_key) DO NOTHING`.
4. Se a chave ja existia: buscar reserva e comparar payload.
   Mesmo pedido retorna a mesma reserva (200); pedido diferente retorna 409.
5. Se a chave e nova: adquirir `FOR NO KEY UPDATE` na linha do imovel.
6. Inserir `bookings` confirmado.
7. PostgreSQL verifica a constraint de exclusao sobre imovel + intervalo.
8. Se houver overlap confirmado, a transacao aborta: nada de reserva ou outbox.
9. Se passar, inserir evento `booking.confirmed` na outbox da mesma transacao.
10. COMMIT; so entao retornar 201 com bookingId e Location.

Nao existe janela entre commit da reserva e criacao da outbox.

## Duas transacoes com datas iguais

```mermaid
sequenceDiagram
    participant A as Guest A / API 1
    participant B as Guest B / API 2
    participant DB as PostgreSQL
    A->>DB: BEGIN + chave A + lock imovel + INSERT [10,15)
    B->>DB: BEGIN + chave B + tenta lock imovel
    Note over DB: B espera admissao; constraint permanece defesa final
    A->>DB: outbox + COMMIT
    DB-->>A: sucesso
    A-->>A: HTTP 201
    B->>DB: lock liberado; INSERT [10,15)
    DB-->>B: exclusion violation 23P01
    B->>DB: ROLLBACK
    B-->>B: HTTP 409
```

A ordem do desenho e ilustrativa, nao promessa de que A chega/venceria primeiro.
Na API final, as novas reservas do mesmo imovel esperam a admissao por lock de
linha antes de disputar a constraint. SQL externo que ignora esse lock ainda
participa da verificacao de exclusao: se uma transacao conflitante comitar,
a concorrente perde; se fizer rollback, outra pode conseguir.

**Nao ha garantia de FIFO/fairness entre guests.** E uma garantia de integridade:
no maximo uma reserva confirmada para a mesma unidade/noites. Sob disputas
complexas tambem podem ocorrer deadlocks/abortos transitorios; tratar como
retries controlados, nunca como sucesso.

`NO KEY UPDATE` e um lock PostgreSQL, nao um lock C# ou Redis. E compativel
com `KEY SHARE` adquirido pelas FKs e mutuamente exclusivo com outra admissao
do mesmo tipo. Reservas em datas disjuntas do mesmo imovel tambem esperam:
e o trade-off escolhido depois do teste real de hotspot. Imoveis diferentes
nao compartilham essa linha e continuam em paralelo.

## Idempotencia nao e exclusao mutua de inventario

- Exclusao: guests **diferentes**, chaves diferentes, mesmo imovel/datas -> um vence.
- Idempotencia: mesmo guest/pedido, mesma chave -> uma criacao, varios replays.
- Mesmo imovel, datas disjuntas -> varias reservas legitimas.
- Mesma chave com outro corpo -> erro de uso do contrato, 409.

O indice UNIQUE em `booking_requests` faz um retry concorrente esperar o primeiro
pedido. A proxima instrucao em READ COMMITTED ve o commit e retorna a reserva.
Se o primeiro pedido abortou, a outra transacao pode reivindicar a chave.

O replay retorna o **estado atual do mesmo recurso**, nao os bytes originais
da primeira resposta. Depois de cancelar, replay retorna `cancelled`; nao
ressuscita a reserva. Recriar exige nova chave e nova decisao de inventario.

## E se a resposta sumir?

O servidor pode comitar e o cliente receber timeout. O cliente nao sabe se
reservou. Repetir com chave nova arrisca uma operacao adicional.

Repita **mesmo corpo e mesma chave**; o banco identifica a operacao. Nunca tratar
timeout como prova de rollback. O servidor nao transforma falhas em confirmacoes.

## Cancelamento

1. BEGIN.
2. SELECT da reserva `FOR UPDATE`.
3. Se ja cancelada, retornar o estado sem emitir outro evento.
4. Caso contrario: mudar status e cancelled_at; inserir evento de cancelamento.
5. COMMIT.

O lock de linha serializa dois cancelamentos da mesma reserva.
A constraint deixa de considerar a reserva apenas quando o cancelamento
comita. Outra reserva concorrente pode esperar ou conflitar, mas nao pode
criar duas confirmadas sobrepostas. Nao apagamos historico.

## O papel do isolation level

READ COMMITTED da snapshots por instrucao. A integridade vem da constraint,
PK/UNIQUE e transacoes com locks pertinentes, nao de snapshots "sempre frescos".
Admissao por imovel reduz contencao; nao e a defesa contra escritores que a ignorem.

SERIALIZABLE e util para invariantes mais amplos que nao cabem numa constraint.
Exige retry de serialization failure e pode abortar mais sob contencao.
Um `SELECT ... FOR UPDATE` em uma busca vazia nao bloqueia magicamente
"todas as futuras linhas que poderiam corresponder".

## Backpressure e retornos

| Resultado | Significado | Acao cliente |
|---|---|---|
| 201 | reserva comitada | guardar bookingId e chave |
| 200 + replay=true | mesma criacao ja existe | usar mesmo bookingId/estado |
| 400 | input invalido | corrigir request |
| 404 | recurso inexistente | corrigir ID |
| 409 | overlap ou chave com outro pedido | entender o problema; nao retry infinito |
| 429 | limite de requests em voo da instancia | backoff com jitter |
| 503 | PostgreSQL indisponivel/timeout/aborto transitorio | retry limitado com a mesma chave |
| 500 | erro inesperado | investigar logs; nao considerar confirmado |

Erro 409 usa um titulo que distingue overlap de chave com outro pedido.
Para clientes de producao, adicione codigos de erro estaveis independentes do idioma.

As permissoes HTTP sao 256 leituras e 128 escritas simultaneas **por instancia**,
sem fila local. Nao sao RPS. O pool PostgreSQL tem limite 50 por instancia,
compartilhado com workers: nem todo request consegue uma conexao imediatamente.
Esses numeros sao defaults didaticos, nao tuning de capacidade.

Duas APIs podem somar ate 100 conexoes. Isso ainda nao reserva margem para
setup do simulador, administracao e outros clientes no `max_connections=100`
do laboratorio. Requests em voo podem aguardar uma conexao do pool; nao
precisam manter uma conexao cada durante toda a operacao HTTP.

## Como a garantia e testada

`verify` faz 200 tentativas, 64 em voo por padrao, e exige 1x201 + 199x409.
Com `--second-url`, distribui disputa e retries por dois processos.
Tambem testa overlaps parciais, intervalos adjacentes, retries, cancelamento e
um INSERT SQL que ignora a API: mesmo assim recebe SQLSTATE `23P01`.

Finalmente, o diagnostico faz um self-join em reservas confirmadas e exige
zero pares com mesmo imovel e `stay && stay`. A consulta e cara e educativa,
nao um endpoint publico para o tamanho hipotetico.

Isso demonstra casos e a regra no banco, nao prova HA sob perda de disco,
failover multi-region ou capacidade de 10M commits/s.
