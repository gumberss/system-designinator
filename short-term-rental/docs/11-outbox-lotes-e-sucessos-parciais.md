# 11. Outbox: UPDATE em lote, confirms e sucessos parciais

Este documento aprofunda o
[publisher do doc 04](04-eventos-e-falhas.md#publisher-pelo-menos-uma-vez)
e a discussao sobre reduzir round trips ao marcar eventos publicados.

**Somente documentacao: UPDATE em lote e persistencia de sucessos parciais
nao foram implementados nem medidos. O codigo atual continua usando um UPDATE
por evento e uma transacao para o lote inteiro.**

## 1. O custo do UPDATE dentro do foreach

O [PublishBatch](../src/ShortTermRental.Api/Messaging.cs) seleciona ate 50 eventos.
Para cada evento, publica com confirm e executa um UPDATE aguardando o banco.
No caminho de sucesso de um lote cheio, temos:

```text
1 SELECT
50 publicacoes sequenciais com publisher confirm
50 UPDATEs sequenciais no PostgreSQL
1 COMMIT
```

E um padrao de N comandos por item, semelhante ao problema N+1.
Cada UPDATE tem custo de comando e round trip, alem do trabalho nas linhas,
indices e WAL. Nao podemos afirmar que e o maior gargalo sem medir: confirms
sequenciais e transacoes abertas durante chamadas de rede tambem custam.

Uma marcacao em lote poderia usar:

```sql
UPDATE outbox
SET published_at = now()
WHERE event_id = ANY(@confirmed_ids)
  AND published_at IS NULL;
```

`@confirmed_ids` representa um parametro PostgreSQL `uuid[]`, nao uma lista
concatenada no SQL. Deve conter somente IDs cuja publicacao foi confirmada.
Sem IDs confirmados, nao ha marcacao a executar.

Passamos de ate 50 UPDATEs para 1. Ainda atualizamos as linhas correspondentes:
nao eliminamos locks, manutencao de indices ou WAL. O ganho esperado e reduzir
comandos e round trips; nao ha ganho de throughput medido para essa alternativa.

## 2. O que significa "deu certo"?

Neste protocolo, sucesso de publicacao exige:

- Publicacao persistente para destino duravel.
- Publisher confirms habilitados e correlacionados com a mensagem correta.
- Confirm positivo, sem retorno por falta de roteamento quando `mandatory=true`.

So terminar a escrita no socket nao basta. Um confirm positivo tambem nao
significa que o consumidor executou ou confirmou o processamento.
Mensagens nao roteaveis podem receber confirm do broker: o tratamento do
retorno e indispensavel, nao apenas habilitar `mandatory`.

| Resultado observado | Acao na outbox |
|---|---|
| Confirm positivo e publicacao roteada | Elegivel para marcar `published_at` |
| NACK do broker | Manter pendente e tratar/repetir com backoff |
| Retorno de mensagem nao roteavel | Manter pendente; investigar topologia |
| Timeout esperando confirm | Manter pendente; resultado e incerto |
| Canal/conexao caiu antes de saber o resultado | Manter pendente |
| Evento ainda nao foi tentado | Manter pendente |

**Timeout nao significa "o RabbitMQ nao recebeu".** O broker pode ter recebido
e persistido a mensagem, mas o confirm se perdeu ou chegou tarde.
Sem evidencia de sucesso, nao marcamos como publicada; tentamos novamente
com o mesmo ID e confiamos na deduplicacao transacional do receptor.

## 3. UPDATE em lote e sucesso parcial sao decisoes diferentes

### Politica atual: transacao do lote inteiro

```text
A publicado -> UPDATE A, ainda sem commit
B publicado -> UPDATE B, ainda sem commit
C falha -> fluxo sai antes do COMMIT -> ROLLBACK
```

Os UPDATEs de A e B sao desfeitos. As publicacoes no broker nao sao desfeitas.
Assim, A, B, C e os demais eventos continuam pendentes no banco.
A e B podem ser republicados; C precisa ser tentado novamente.

Trocar os UPDATEs individuais por um UPDATE ao final, mas continuar abortando
antes dele em qualquer falha, **mantem essa politica de rollback do lote**.
O UPDATE por evento atual nao registra progresso duravel por evento.

### Politica proposta: persistir sucessos parciais

```text
A publicado e confirmado -> adicionar A aos sucessos
B publicado e confirmado -> adicionar B aos sucessos
C teve timeout -> nao adicionar C; interromper se o canal nao for confiavel
D ainda nao foi tentado -> nao adicionar D

UPDATE em lote somente de A e B
COMMIT da marcacao

C e D continuam pendentes para uma proxima tentativa
```

Essa proposta faz sentido: reduz republicacoes desnecessarias dos sucessos
ja conhecidos, sem descartar falhas ou resultados incertos.
O sucesso so fica duravel depois do commit dessa marcacao.

Nao e necessario continuar publicando o restante do lote depois de um erro.
Em falha de canal/conexao, e mais seguro interromper, salvar os confirms ja
conhecidos quando possivel e recriar o canal para as proximas tentativas.
Se houver publicacao concorrente, eventos em voo sem resultado conhecido
tambem continuam pendentes; o conjunto de sucessos exige correlacao correta.

## 4. Cuidados para salvar o progresso parcial

O objetivo nao e esconder erros em um catch e retornar sucesso para o lote.
Precisamos distinguir quantidade confirmada, pendencias e causa da falha,
com logs/metricas e backoff.

Um fluxo conceitual, mantendo os locks do lote, seria:

```text
BEGIN no PostgreSQL
Selecionar pendencias com FOR UPDATE SKIP LOCKED
Publicar e acumular IDs com confirm positivo
Se a publicacao falhar, parar e registrar a falha
Se a transacao do banco ainda estiver valida:
  UPDATE somente dos IDs confirmados
  COMMIT
Liberar recursos e recuperar o publisher com backoff
```

Uma falha no RabbitMQ nao aborta automaticamente a transacao PostgreSQL.
Por isso, pode ser possivel salvar A e B nela. Entretanto:

- Erro SQL pode deixar a transacao abortada; nao basta tentar outro UPDATE.
- Perda da conexao PostgreSQL impede salvar o progresso nessa transacao.
- Cancelamento do processo pode impedir UPDATE/COMMIT.
- Falha na resposta do COMMIT pode deixar o resultado incerto.
- Encerramento gracioso pode tentar salvar progresso com prazo limitado,
  mas a recuperacao nao pode depender de conseguir faze-lo.

Nesses casos, os sucessos podem continuar pendentes e ser republicados.
Nunca marcar como publicado sem confirm apenas para "destravar" o lote.
Se os locks forem liberados antes da marcacao, outra instancia pode pegar os
mesmos eventos. Esse desenho precisa de coordenacao explicita, por exemplo
claims/leases com recuperacao de expiracao; nao basta retirar a transacao.

## 5. Por que ainda existem duplicatas?

Mesmo persistindo sucessos parciais, continua existindo esta janela:

```text
RabbitMQ confirmou A
Processo morreu antes de comitar published_at de A
Outro publisher encontra A pendente e republica
```

Nao temos uma transacao atomica entre RabbitMQ e PostgreSQL.
A politica de sucessos parciais reduz repeticoes, mas nao entrega exactly-once.

A republicacao deve preservar `event_id` e payload original.
O receptor grava esse ID na inbox e aplica seu efeito na mesma transacao.
Se o evento ja foi processado, nao repete o efeito; confirma a entrega depois
do commit. Veja o
[consumidor idempotente do doc 04](04-eventos-e-falhas.md#consumidor-efeito-transacional-idempotente).

Retencao da inbox deve cobrir a janela de republicacao/replay. Efeitos externos,
como pagamento ou email, exigem mecanismos adicionais: deduplicacao local
nao torna uma chamada externa atomica com o banco.

## 6. Falha permanente nao pode ficar esquecida

Manter pendente protege contra perda silenciosa, mas nao garante recuperacao
se o problema nunca for corrigido.
Topologia errada, contrato invalido ou evento que sempre falha exigem:

- Logs e classificacao da causa.
- Retry com backoff, evitando loop apertado.
- Alertas de idade das pendencias e contagem de tentativas.
- Quarentena/reprocessamento operacional quando necessario, sem chamar
  um evento nao publicado de sucesso.

Eventos que falham sempre tambem podem ocupar repetidamente os primeiros
lotes e prejudicar outros eventos. Scheduling de retry e tratamento de falhas
permanentes precisam ser projetados, nao resolvidos descartando mensagens.
Essas capacidades de producao nao foram adicionadas neste documento.

## 7. O que medir antes de implementar?

Comparar UPDATE individual e em lote com o mesmo tamanho de lote, carga e
politica de confirms:

- Eventos publicados por segundo e idade da outbox.
- Tempo em publicacao/confirm versus tempo em SQL.
- Duracao da transacao e ocupacao do pool.
- Duplicatas observadas e convergencia da projecao.

Para validar sucesso parcial, provocar falha depois de alguns confirms e
verificar que somente esses IDs ficam marcados apos commit. Simular tambem
timeout incerto, crash antes da marcacao e falha de commit: pendencias devem
ser recuperadas, e republicacoes nao podem duplicar o efeito no consumidor.

**Conclusao:** marcar em lote somente os eventos confirmados e deixar falhas,
timeouts e eventos nao tentados pendentes e uma politica coerente.
O UPDATE em lote melhora eficiencia; o commit dos sucessos parciais registra
progresso. A idempotencia do receptor continua obrigatoria nos dois casos.

## Referencias

- [Doc 04: publisher, inbox e matriz de falhas](04-eventos-e-falhas.md)
- [Doc 10: retry duravel e republicacao idempotente](10-outbox-e-retry-duravel.md)
- [RabbitMQ: publisher confirms e mensagens nao roteaveis](https://www.rabbitmq.com/docs/confirms)
- [PostgreSQL: comparacao ANY com arrays](https://www.postgresql.org/docs/17/functions-comparisons.html#FUNCTIONS-COMPARISONS-ANY-SOME)
