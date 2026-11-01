# 10. Por que outbox? E quando uma entrada duravel pode substitui-la?

Este documento aprofunda a discussao do
[doc 04: eventos, falhas e duplicatas](04-eventos-e-falhas.md).
O doc 04 explica o publisher, a inbox e a operacao implementados no laboratorio.
Aqui comparamos duas formas de recuperar a publicacao depois de uma falha.

**A alternativa sem outbox descrita abaixo e conceitual, nao foi implementada
nem medida neste repositorio. O sistema atual continua usando HTTP + outbox.**

## 1. Idempotencia e entrega confiavel sao problemas diferentes

| Mecanismo | Responsabilidade |
|---|---|
| Idempotencia da operacao de entrada | Nao criar duas reservas para o mesmo pedido |
| Outbox ou outro mecanismo duravel de recuperacao | Retomar a publicacao de um evento depois de falhas |
| Idempotencia no consumidor de saida | Nao aplicar duas vezes o efeito do mesmo evento |

Uma chave de idempotencia permite reconhecer um pedido repetido. Ela nao faz
um retry acontecer e nao registra, sozinha, quais eventos ainda precisam sair.
Ter deduplicacao no consumidor tambem nao recupera um evento nunca publicado.

## 2. Por que usamos outbox no fluxo HTTP?

PostgreSQL e RabbitMQ nao compartilham uma transacao atomica neste sistema.
Publicar diretamente cria duas janelas possiveis:

```text
Publicar primeiro:
  RabbitMQ aceita evento -> transacao da reserva sofre rollback
  Resultado: evento descreve uma reserva que nao comitou.

Comitar primeiro:
  Reserva comita -> processo morre antes de publicar
  Resultado: reserva existe, mas o evento pode nunca sair.
```

Se a transacao realmente sofreu rollback, nem a reserva nem a chave de
idempotencia gravadas nela ficam persistidas. O retry tenta criar novamente.
Se o COMMIT aconteceu, mas sua resposta se perdeu, o resultado e incerto para
o cliente: o retry com a mesma chave permite descobrir a reserva existente.

A proposta "no retry, encontro a reserva e republico o evento" ajuda na segunda
janela, **mas somente se o cliente repetir o pedido**. Ele pode fechar o app,
desistir ou ja ter recebido sucesso antes de uma publicacao posterior falhar.
Nao podemos depender desse retry para recuperar eventos obrigatorios.

Com outbox, gravamos a reserva e a intencao de publicar juntas:

```text
BEGIN
  Gravar chave de idempotencia
  Gravar reserva
  Gravar evento original na outbox
COMMIT

Worker independente:
  Buscar eventos pendentes
  Publicar e esperar publisher confirm
  Marcar publicacao no banco
```

Ou tudo comita, ou tudo sofre rollback. Depois do commit, um worker consegue
retomar a publicacao sem depender de outro request HTTP.

No codigo atual, [BookingStore.Create](../src/ShortTermRental.Api/BookingStore.cs)
grava a reserva e chama `InsertEvent` na mesma transacao. O cancelamento tambem
grava seu evento na mesma transacao da mudanca de status.
O [OutboxPublisher](../src/ShortTermRental.Api/Messaging.cs) publica as pendencias.
Um replay HTTP retorna a reserva existente; ele nao cria um novo evento.

## 3. E se o pedido chegar por mensageria?

Imagine uma entrada `booking.requested`, que pede para criar uma reserva.
Ela e uma mensagem diferente da saida `booking.confirmed`, que informa o
resultado. Uma mensagem pode transportar um comando; chegar via broker nao
transforma automaticamente o pedido em uma reserva ja confirmada.

Se essa entrada for duravel e recuperavel, ela pode substituir a outbox como
**gatilho de retry da operacao completa**, incluindo a publicacao da saida:

```text
1. Receber booking.requested com ACK manual.
2. Abrir transacao no PostgreSQL.
3. Criar a reserva ou recuperar o resultado idempotente do mesmo pedido.
4. COMMIT.
5. Publicar o evento de saida original e esperar publisher confirm.
6. Somente entao dar ACK na entrada.
```

O banco nao precisa ficar com uma transacao aberta enquanto espera o broker:
o passo 4 ja terminou a transacao. A entrada continua sem ACK ate o passo 6.
Mensagens sem ACK sao reentregues quando o canal/conexao termina; nao e apenas
a existencia de uma excecao no callback que garante essa reentrega.

### Falhas e retomada

| Ponto da falha | Estado persistido | Acao na reentrega |
|---|---|---|
| Antes do commit | Reserva nao comitou | Tentar a operacao novamente |
| Commit aconteceu, resposta do banco se perdeu | Resultado pode existir | Resolver pela chave idempotente; nao criar outra reserva |
| Depois do commit, antes de publicar | Reserva existe; saida falta | Recuperar resultado e publicar a saida |
| Broker recebeu a saida, confirm se perdeu | Saida pode ja existir | Republicar com o mesmo ID |
| Confirm recebido, antes do ACK da entrada | Saida existe; entrada pode voltar | Republicar com o mesmo ID e depois dar ACK |
| Depois do ACK da entrada | Protocolo terminou | Duplicatas independentes ainda exigem idempotencia |

Nao ha atomicidade entre commit, publicacao e ACK. O desenho aceita duplicatas
e as trata, em vez de prometer exactly-once no transporte.

**O erro principal seria deduplicar a entrada e retornar imediatamente:**

```text
ERRADO neste desenho sem outbox:
  "A reserva ja existe; dar ACK e sair."

CORRETO:
  "A reserva ja existe; recuperar o resultado original,
   publicar a saida, esperar confirm e so entao dar ACK."
```

A idempotencia impede repetir o efeito no banco, nao pode pular uma publicacao
que talvez ainda esteja faltando.

## 4. Identidade e conteudo precisam sobreviver ao retry

Nao basta executar `Guid.NewGuid()` em cada republicacao: o consumidor veria
IDs diferentes e poderia aplicar o mesmo efeito varias vezes.

- A entrada precisa de uma chave de operacao estavel, preservada em reentregas
  e republicacoes de retry. `DeliveryTag` do RabbitMQ nao e essa chave.
- A mesma chave com outro pedido deve ser rejeitada, nao tratada como replay.
- A saida precisa do mesmo `event_id` e do mesmo conteudo logico em cada retry.
- Para varias saidas, cada evento precisa de identidade propria e estavel.
- Entregas concorrentes da mesma entrada precisam usar deduplicacao atomica
  no banco; um "SELECT e depois INSERT" sem protecao nao basta.

O resultado original pode ser persistido junto com a reserva ou reconstruido
de informacoes imutaveis, com uma regra deterministica de identidade.
Reconstruir a confirmacao cegamente a partir do estado atual e perigoso:
a reserva pode ter sido cancelada entre a primeira tentativa e o retry.
Timestamps e versoes originais tambem nao podem mudar a cada tentativa.

Persistir o resultado para replay nao e necessariamente uma outbox. Mas se
tambem registramos eventos pendentes e um worker os busca independentemente
da entrada, estamos implementando a essencia da outbox, mesmo com outro nome.

## 5. Idempotencia de quem recebe a saida

O consumidor de `booking.confirmed` continua precisando de inbox/deduplicacao:

```text
Receber evento, sem ACK
BEGIN
  Registrar event_id na inbox com unicidade
  Se for novo, aplicar efeito na projecao
COMMIT
ACK
```

Inbox e efeito devem comitar juntos. Se o consumidor morrer depois do commit
e antes do ACK, a reentrega encontra o mesmo ID e nao repete o efeito.
Esse e o protocolo usado pelo
[StatisticsConsumer](../src/ShortTermRental.Api/Messaging.cs) do laboratorio.

A garantia cobre o efeito transacional neste banco. Pagamentos, email e outros
efeitos externos precisam de mecanismos proprios de idempotencia/recuperacao;
uma inbox local nao torna uma chamada externa atomica com o commit.

## 6. Condicoes para dispensar outbox nessa alternativa

O desenho exige um contrato operacional, nao apenas um bloco `try/catch`:

1. A entrada foi publicada de forma confiavel e permanece duravel no broker.
2. ACK manual somente depois da publicacao confirmada da saida.
3. Saida persistente, destino duravel, confirms e tratamento de mensagem
   nao roteavel; confirm nao significa que o consumidor executou.
4. Retomada da publicacao tambem no caminho de replay da entrada.
5. IDs estaveis, resultado original recuperavel e deduplicacao transacional.
6. Retry com backoff e recuperacao explicita de entradas enviadas para DLQ.
7. Retencao das chaves/inbox suficiente para a janela de retry e replay.
8. Monitoramento de mensagens sem ACK, idade do backlog e pendencias na DLQ.

TTL, limite de entregas, descarte ou perda do broker podem remover o unico
gatilho de recuperacao. Se a reserva ja comitou e a entrada vai para DLQ,
o evento de saida pode continuar faltando. DLQ nao significa operacao concluida:
exige alerta, inspecao e replay/reconciliacao.

Se o retry usa outra fila, a transferencia precisa ser confiavel: publicar
o retry, esperar confirm e depois confirmar a entrada original. Uma falha
nessa transferencia tambem pode gerar duplicatas.

## 7. Comparacao e motivo para manter outbox

| Aspecto | Entrada duravel, sem outbox | Entrada duravel + outbox |
|---|---|---|
| Momento do ACK de entrada | Depois de publicar a saida com confirm | Depois de comitar efeito + outbox |
| Broker de saida indisponivel | Entrada permanece pendente ou vai para retry/DLQ | Worker de saida acumula pendencias na outbox |
| Recuperacao apos commit | Depende de reentrega/replay da entrada | Worker encontra a pendencia no banco |
| Registro do evento original | Precisa ser recuperavel por outro meio | ID e payload persistidos na outbox |
| Duplicatas de saida | Possiveis; consumidor deduplica | Possiveis; consumidor deduplica |
| Custo operacional | Gerir retry, ACK, prefetch e DLQ da entrada | Gerir publisher, tabela, retencao e backlog |

Sem outbox, uma falha de saida prolonga o processamento da entrada e pode
ocupar prefetch, causar head-of-line blocking ou exigir filas de retry.
Com outbox, processamento de entrada e entrega de saida ficam desacoplados,
embora a capacidade do banco e o crescimento da outbox continuem limitados.
Nenhum desenho elimina a necessidade de operacao e recuperacao.

**Conclusao:** podemos dispensar outbox se a mensagem de entrada garantir
retry duravel da operacao inteira, usando idempotencia da reserva +
republicacao estavel da saida + idempotencia transacional do receptor.
A responsabilidade de recuperacao foi transferida para a entrada; nao sumiu.

No fluxo HTTP deste repositorio, mantemos outbox porque nao existe essa
entrada duravel e nao queremos depender de retries do cliente.
Outras alternativas incluem CDC ou um log duravel com reconciliacao, desde
que preservem a intencao e permitam recuperar todas as publicacoes necessarias.

## Referencias

- [Doc 04: implementacao, confirms, inbox e falhas](04-eventos-e-falhas.md)
- [Doc 02: modelagem de outbox, idempotencia e projecoes](02-modelagem-de-dados.md)
- [RabbitMQ: consumer ACKs e publisher confirms](https://www.rabbitmq.com/docs/confirms)
- [RabbitMQ: confiabilidade e recuperacao](https://www.rabbitmq.com/docs/reliability)
