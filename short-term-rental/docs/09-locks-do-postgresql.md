# 9. Locks do PostgreSQL: diferencas e quando usar

Este guia compara locks de linha, tabela, advisory e mecanismos internos.
Os exemplos se referem ao PostgreSQL 17 deste laboratorio. Nao alteramos a
implementacao: para o fluxo de reservas, veja
[concorrencia](03-concorrencia.md) e
[BookingStore.cs](../src/ShortTermRental.Api/BookingStore.cs).

## 1. Antes dos nomes: o que um lock protege?

Um lock coordena operacoes concorrentes sobre um recurso. Duas transacoes
podem acessar esse recurso ao mesmo tempo se seus modos forem compativeis;
quando nao sao, uma normalmente espera.

| Categoria | Recurso / finalidade |
|---|---|
| Locks de linha | Coordenar alteracoes ou leituras protegidas de linhas existentes |
| Locks de tabela | Coordenar acesso, escrita e mudancas estruturais da tabela |
| Advisory locks | Coordenar recursos logicos definidos pela aplicacao |
| Predicate locks | Detectar dependencias no isolamento SERIALIZABLE |
| Locks internos | Proteger estruturas internas, buffers e memoria compartilhada |

Uma operacao pode adquirir mais de uma categoria. `SELECT ... FOR UPDATE`
adquire lock de tabela e de linhas; nao e uma escolha entre um ou outro.

**Lock nao e indice, pool ou constraint.** O pool limita conexoes; o indice
organiza buscas; a constraint impoe uma regra de dados; o lock coordena acesso.

## 2. Os quatro modos de lock de linha

### `FOR UPDATE`: o mais restritivo

```sql
SELECT *
FROM bookings
WHERE id = @bookingId
FOR UPDATE;
```

Impede outra transacao de atualizar, apagar ou adquirir qualquer um dos quatro
modos de lock de linha na mesma linha enquanto o lock estiver ativo.
Um SELECT comum ainda pode ler sua versao visivel via MVCC.

**Quando usar:** ler o estado de uma linha e depois decidir sua alteracao,
com exclusividade forte. No projeto, dois cancelamentos da mesma reserva
precisam observar e alterar o estado de forma coordenada.

O PostgreSQL tambem adquire esse modo automaticamente em DELETE e em UPDATE
que altere determinadas colunas-chave. Na versao 17, essa classificacao envolve
colunas com indice UNIQUE elegivel para ser referenciado por uma foreign key;
nao significa que qualquer coluna com nome `id` e especial.

### `FOR NO KEY UPDATE`: exclusividade compativel com referencias

```sql
SELECT id
FROM properties
WHERE id = @propertyId
FOR NO KEY UPDATE;
```

Conflita com UPDATE, NO KEY UPDATE e SHARE de outras transacoes na mesma linha,
mas permite KEY SHARE.

**Quando usar:** coordenar escritores sem impedir a protecao de referencias
por foreign keys. UPDATEs que nao exigem o modo mais forte normalmente
adquirem NO KEY UPDATE automaticamente.

No projeto, funciona como admissao de novas reservas por imovel. Duas novas
reservas da mesma unidade esperam entre si; unidades diferentes seguem em paralelo.
Datas disjuntas do mesmo imovel tambem esperam: e um trade-off de contencao.

O nome nao executa um UPDATE nem e uma proibicao sintatica de alterar a chave.
Descreve a forca do lock solicitado. Uma operacao posterior pode precisar
de um lock mais forte.

### `FOR SHARE`: leitura protegida contra alteracoes

```sql
SELECT *
FROM properties
WHERE id = @propertyId
FOR SHARE;
```

Permite outros SHARE e KEY SHARE na mesma linha. Conflita com UPDATE e
NO KEY UPDATE, portanto impede alteracoes/exclusoes por outras transacoes.

**Quando usar:** sua transacao precisa que uma linha existente nao seja
modificada enquanto realiza outro trabalho, mas varios leitores podem
compartilhar essa protecao.

Nao usar SHARE como garantia de que todos os leitores conseguirao depois
atualizar a linha: upgrades concorrentes podem causar deadlock.

### `FOR KEY SHARE`: protege principalmente a chave/existencia

```sql
SELECT id
FROM properties
WHERE id = @propertyId
FOR KEY SHARE;
```

Conflita com UPDATE, mas e compativel com KEY SHARE, SHARE e NO KEY UPDATE.
Bloqueia exclusao ou UPDATE que exija o lock forte por modificar chave relevante,
mas permite updates que adquiram somente NO KEY UPDATE.

**Quando usar:** proteger a referencia a um registro, sem exigir que todos
os seus outros campos permanecam inalterados. E o modo usado pelo PostgreSQL
quando necessario nas verificacoes de foreign keys.

Nao e o mesmo que FOR SHARE: outro processo pode, por exemplo, alterar um
metadado nao-chave enquanto voce mantem KEY SHARE.

## 3. Matriz completa de conflitos de linha

Para transacoes diferentes sobre **a mesma linha**:

| Solicitado / existente | KEY SHARE | SHARE | NO KEY UPDATE | UPDATE |
|---|---|---|---|---|
| KEY SHARE | Compativel | Compativel | Compativel | Espera |
| SHARE | Compativel | Compativel | Espera | Espera |
| NO KEY UPDATE | Compativel | Espera | Espera | Espera |
| UPDATE | Espera | Espera | Espera | Espera |

"Espera" considera a politica padrao. NOWAIT pode transformar isso em erro,
e SKIP LOCKED pode pular a linha. Uma transacao normalmente nao conflita
com os locks que ela mesma ja possui.

### Um SELECT comum nao e FOR SHARE

```sql
SELECT * FROM bookings WHERE id = @bookingId;
```

Esse SELECT nao adquire um desses quatro locks de linha. MVCC permite ler
a versao visivel conforme o isolamento, sem normalmente esperar por um
UPDATE nao comitado.

Ele ainda adquire lock de tabela ACCESS SHARE: por isso pode esperar
por uma operacao com ACCESS EXCLUSIVE. "Leituras nao bloqueiam escritas"
e uma simplificacao de MVCC, nao uma regra absoluta para qualquer SQL.

### Quanto tempo dura?

Locks de linha/tabela normalmente duram ate COMMIT ou ROLLBACK da transacao.
Fechar o reader ou descartar o NpgsqlCommand nao libera o lock.
Em autocommit, a transacao termina junto da instrucao; nao protege uma
instrucao posterior independente.

Rollback para savepoint libera locks adquiridos depois daquele savepoint.
Advisory locks de sessao possuem outra duracao, explicada adiante.

Em READ COMMITTED, uma leitura com lock que espera pode retornar a linha
atualizada pelo concorrente, ou nenhuma linha se ela foi apagada.
Em REPEATABLE READ/SERIALIZABLE, alteracoes desde o snapshot podem causar
erro em vez desse comportamento. Isolamento e locking precisam ser
considerados juntos.

## 4. Por que NO KEY UPDATE na admissao deste projeto?

Antes de bloquear properties, a criacao grava booking_requests, que possui
FK para o imovel. Duas transacoes podem ter KEY SHARE no mesmo registro
referenciado.

Se ambas tentarem elevar a protecao para FOR UPDATE:

```text
A possui KEY SHARE; B possui KEY SHARE.
A pede UPDATE e espera o KEY SHARE de B.
B pede UPDATE e espera o KEY SHARE de A.
Resultado possivel: deadlock.
```

NO KEY UPDATE e compativel com esses KEY SHARE, mas duas admissoes
NO KEY UPDATE nao coexistem. Uma avanca, outra espera.

Isso evita **esse ciclo especifico**, nao garante ausencia de qualquer
deadlock em todos os fluxos futuros. A constraint EXCLUDE continua sendo
a defesa contra reservas sobrepostas; veja
[GiST e exclusao](08-indice-gist-e-exclusao.md).

## 5. NOWAIT e SKIP LOCKED: politicas de espera

Nao sao novos modos de lock. Modificam o comportamento da aquisicao
dos locks de linha solicitados.

### Padrao: esperar

```sql
SELECT * FROM bookings WHERE id = @bookingId FOR UPDATE;
```

Aguarda o lock, sujeito a timeouts, cancelamento ou deteccao de deadlock.

### NOWAIT: erro imediato

```sql
SELECT * FROM bookings WHERE id = @bookingId FOR UPDATE NOWAIT;
```

Se a linha estiver bloqueada de forma incompativel, retorna
`55P03` (`lock_not_available`) em vez de aguardar.

Util para uma UX de "recurso ocupado" ou retry limitado.
Nao equivale a "reserva ja ocupada": lock e estado de negocio sao coisas diferentes.

### SKIP LOCKED: buscar outro trabalho

```sql
SELECT event_id
FROM outbox
WHERE published_at IS NULL
ORDER BY created_at
LIMIT 50
FOR UPDATE SKIP LOCKED;
```

Pula linhas cujo lock nao consegue adquirir imediatamente. Workers diferentes
podem pegar lotes distintos. E usado pelo
[publisher](../src/ShortTermRental.Api/Messaging.cs).

Nao promete uma visao completa/consistente das linhas elegiveis nem FIFO global.
**Nao use para concluir disponibilidade:** ignorar uma reserva bloqueada
nao significa que as noites estao livres.

NOWAIT/SKIP LOCKED no SELECT se aplicam aos locks de linha; o lock de tabela
necessario ainda pode esperar. Nao sao garantia de que toda a instrucao
terminara imediatamente.

## 6. Os oito modos de lock de tabela

Os nomes ROW SHARE e ROW EXCLUSIVE sao historicos: **tambem sao locks de tabela**.
A diferenca entre os modos e o conjunto de outros modos com que conflitam,
nao apenas a aparencia do nome.

| Modo | Operacoes tipicas que o adquirem |
|---|---|
| ACCESS SHARE | SELECT comum |
| ROW SHARE | SELECT com FOR UPDATE / NO KEY UPDATE / SHARE / KEY SHARE na tabela |
| ROW EXCLUSIVE | INSERT, UPDATE, DELETE, MERGE |
| SHARE UPDATE EXCLUSIVE | VACUUM sem FULL, ANALYZE, CREATE INDEX CONCURRENTLY |
| SHARE | CREATE INDEX sem CONCURRENTLY |
| SHARE ROW EXCLUSIVE | CREATE TRIGGER e algumas formas de ALTER TABLE |
| EXCLUSIVE | REFRESH MATERIALIZED VIEW CONCURRENTLY |
| ACCESS EXCLUSIVE | DROP TABLE, TRUNCATE, VACUUM FULL e muitas formas de ALTER TABLE |

Esses sao exemplos, nao a lista completa de comandos. O modo de ALTER TABLE
depende da suboperacao. REFRESH MATERIALIZED VIEW CONCURRENTLY adquire
EXCLUSIVE; sem CONCURRENTLY, adquire ACCESS EXCLUSIVE.

### Matriz completa de conflitos de tabela

`X` = conflita; `-` = compativel entre transacoes diferentes na mesma tabela.

Abreviacoes: AS=ACCESS SHARE, RS=ROW SHARE, RX=ROW EXCLUSIVE,
SUE=SHARE UPDATE EXCLUSIVE, S=SHARE, SRX=SHARE ROW EXCLUSIVE,
E=EXCLUSIVE, AX=ACCESS EXCLUSIVE.

| Solicitado / existente | AS | RS | RX | SUE | S | SRX | E | AX |
|---|---|---|---|---|---|---|---|---|
| AS | - | - | - | - | - | - | - | X |
| RS | - | - | - | - | - | - | X | X |
| RX | - | - | - | - | X | X | X | X |
| SUE | - | - | - | X | X | X | X | X |
| S | - | - | X | X | - | X | X | X |
| SRX | - | - | X | X | X | X | X | X |
| E | - | X | X | X | X | X | X | X |
| AX | X | X | X | X | X | X | X | X |

Consequencias praticas:

- Varios INSERTs podem possuir ROW EXCLUSIVE ao mesmo tempo.
  Isso nao serializa toda a escrita da tabela.
- CREATE INDEX comum adquire SHARE e conflita com escrita ROW EXCLUSIVE.
- CREATE INDEX CONCURRENTLY reduz esse bloqueio, mas nao dispensa locks,
  esperas ou restricoes operacionais.
- ACCESS EXCLUSIVE conflita inclusive com SELECT comum.
- EXCLUSIVE permite SELECT comum (ACCESS SHARE), mas nao leitores com
  lock de linha que precisem de ROW SHARE.

Tambem e possivel pedir um modo explicitamente:

```sql
BEGIN;
LOCK TABLE bookings IN SHARE MODE NOWAIT;
-- Trabalho que precisa dessa protecao; escritas concorrentes ficam bloqueadas.
ROLLBACK;
```

Esse e um exemplo didatico, nao a estrategia de reserva do projeto.
Lock de tabela seria amplo demais para coordenar somente um imovel.

## 7. Advisory locks: o recurso e definido pela aplicacao

Um advisory lock nao precisa corresponder a uma linha. A aplicacao escolhe
uma chave numerica para um recurso logico, como "importacao do tenant 123".

| Variante | Funcao de exemplo | Duracao / comportamento |
|---|---|---|
| Exclusivo transacional | `pg_advisory_xact_lock(123::bigint)` | Ate terminar a transacao |
| Compartilhado transacional | `pg_advisory_xact_lock_shared(123::bigint)` | Ate terminar a transacao; outros shared podem coexistir |
| Exclusivo de sessao | `pg_advisory_lock(123::bigint)` | Ate unlock explicito ou fim da sessao |
| Compartilhado de sessao | `pg_advisory_lock_shared(123::bigint)` | Mesma duracao de sessao |
| Tentativa sem esperar | `pg_try_advisory_xact_lock(123::bigint)` | Retorna boolean indicando aquisicao |

Existem tambem variantes `try` de sessao/compartilhadas e assinaturas com
dois inteiros. Exclusivo conflita com shared/exclusivo do mesmo recurso;
shared e compativel com outros shared.

```sql
BEGIN;
SELECT pg_advisory_xact_lock(123::bigint);
-- Trabalho protegido pelo protocolo da aplicacao.
COMMIT;
```

**Limite principal:** outros escritores precisam cooperar. Um INSERT que
ignore o advisory lock nao e bloqueado automaticamente por ele.
E necessario definir chaves sem colisao indevida entre recursos/subsistemas.

### Cuidado com locks de sessao e connection pooling

Lock de sessao sobrevive ao rollback da transacao. Fechar uma conexao logica
pooled nao equivale a encerrar a sessao fisica do PostgreSQL; drivers podem
executar resets na devolucao/reutilizacao, mas nao dependa disso como protocolo
de negocio. Use desbloqueio explicito e considere a configuracao do driver.

Locks de sessao tambem podem ser adquiridos repetidamente pelo mesmo dono:
e necessario um unlock correspondente para cada aquisicao.
Para trabalho limitado a uma transacao, a variante `_xact_` costuma ser mais
simples e evita acoplar a exclusao ao ciclo de vida da sessao do pool.

## 8. Predicate locks no SERIALIZABLE

O PostgreSQL implementa Serializable Snapshot Isolation (SSI) com rastreamento
de dependencias, incluindo predicate locks observaveis como `SIReadLock`.

Eles registram leituras relevantes para detectar execucoes que nao poderiam
equivaler a uma ordem serial. **Nao bloqueiam escritores como FOR UPDATE**;
o banco pode abortar uma transacao com `40001` (`serialization_failure`).

A aplicacao precisa fazer retry limitado da transacao inteira. Eles podem
ter granularidade de tupla/pagina/relacao e permanecer algum tempo depois
do commit, ate transacoes sobrepostas relevantes terminarem.

Nao confundir com "bloquear todas as linhas que combinam com meu WHERE".
Um SELECT FOR UPDATE que nao encontra nenhuma linha tambem nao cria,
por si so, um lock de intervalo que proiba futuros INSERTs correspondentes.

## 9. Locks internos, de pagina e esperas de transacao

O PostgreSQL usa mecanismos internos como:

- locks de pagina de curta duracao para acesso a estruturas;
- lightweight locks (`LWLock`) para memoria compartilhada/buffers;
- buffer pins, que podem impedir certas operacoes sobre um buffer;
- locks/esperas por transaction IDs quando precisa aguardar outra transacao.

Nao sao clausulas SQL que voce escolhe como FOR UPDATE. Alguns aparecem em
`pg_locks`, outros em `wait_event_type`/`wait_event` de `pg_stat_activity`.
Nao interpretar todo wait como lock pessimista colocado pela aplicacao.

## 10. Deadlock, espera e timeout nao sao a mesma coisa

| Situacao | Significado | Acao |
|---|---|---|
| Espera de lock | Outra transacao tem protecao incompativel | Aguardar dentro do prazo ou reduzir contencao |
| Deadlock, `40P01` | Ciclo de esperas sem possibilidade de avancar | Banco aborta um participante; retry limitado da transacao |
| NOWAIT / lock timeout, `55P03` | Nao conseguiu lock conforme politica/prazo | Tratar como ocupacao transitoria, sem confirmar |
| Serialization failure, `40001` | Execucao nao pode preservar isolamento serializavel | Reexecutar transacao inteira |
| Exclusion violation, `23P01` | Dados violariam a regra de overlap | Conflito de inventario, nao retry cego |
| Pool esgotado | Cliente aguarda uma conexao disponivel | Dimensionar pool/admissao; nao necessariamente lock no banco |

Boas praticas: transacoes curtas, ordem consistente para bloquear multiplas
linhas, nao fazer chamadas externas longas enquanto segura locks, deadlines
e retries limitados com jitter. Nao manter lock aberto durante checkout/pagamento.

Configuracoes de prazo diferem:

- `lock_timeout`: limita espera para adquirir locks.
- `statement_timeout`: limita tempo total da instrucao no servidor.
- `deadlock_timeout`: controla quando o banco verifica deadlock apos espera;
  nao e um prazo maximo geral de lock.
- Timeout/Command Timeout do Npgsql: prazos no cliente para conexao/comandos,
  nao configuracao automatica desses parametros do servidor.

O publisher deste laboratorio segura transacao enquanto publica um lote
no broker. E uma simplificacao com custo explicitado em
[eventos e falhas](04-eventos-e-falhas.md), nao uma recomendacao universal.

## 11. Como observar locks sem alterar dados

Queries para executar no psql do laboratorio, com permissoes apropriadas:

```sql
SELECT pid, state, wait_event_type, wait_event,
       pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY pid;
```

Mostra quem esta esperando e os PIDs que o bloqueiam. Uma sessao
`idle in transaction` pode continuar segurando locks mesmo sem executar SQL.

```sql
SELECT pid, locktype, mode, granted, relation::regclass AS relation,
       transactionid
FROM pg_locks
WHERE pid IN (
    SELECT pid FROM pg_stat_activity WHERE datname = current_database()
)
ORDER BY pid, granted, locktype;
```

`granted=false` indica aquisicao pendente. Nem todos os locks de linha
aparecem como uma linha `tuple` em pg_locks: o PostgreSQL guarda parte
dessa informacao nas tuplas e pode mostrar a espera por transaction ID.
Ausencia de `tuple` nao prova ausencia de contencao de linha.

Nao termine sessoes de outros usuarios/projetos para "resolver" a espera.
Investigue dono, duracao e fluxo antes de tomar medidas operacionais.

## 12. Resumo para o ShortTermRental

| Caminho | Escolha | Motivo |
|---|---|---|
| Criacao | NO KEY UPDATE em properties | Admissao por imovel compativel com FK KEY SHARE |
| Cancelamento | UPDATE em bookings | Coordenar leitura e mudanca do estado |
| Outbox | UPDATE + SKIP LOCKED | Dividir lotes entre publishers |
| FK | KEY SHARE quando necessario | Proteger existencia/chave do registro referenciado |
| Disponibilidade | SELECT comum + MVCC | Snapshot, nao posse do inventario |
| Garantia anti-overbooking | EXCLUDE apoiada em GiST | Rejeitar mesmo imovel com noites sobrepostas |

**Escolha o lock pela compatibilidade necessaria, nao pelo nome mais forte.
E mantenha a regra de integridade separada da estrategia de admissao.**

Referencias oficiais PostgreSQL 17:

- [Explicit locking e matrizes de conflitos](https://www.postgresql.org/docs/17/explicit-locking.html)
- [SELECT e locking clauses](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [LOCK TABLE](https://www.postgresql.org/docs/17/sql-lock.html)
- [Advisory lock functions](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)
- [Isolamento SERIALIZABLE](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-SERIALIZABLE)
- [pg_locks](https://www.postgresql.org/docs/17/view-pg-locks.html)
