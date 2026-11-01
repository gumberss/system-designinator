# 7. Resultados realmente verificados

Execucao local em **31/10/2026**, Windows + Docker Desktop/Linux containers.
Docker server 27.2.0, com **5 CPUs e 5.161.676.800 bytes de memoria**
(aproximadamente 4,81 GiB) disponiveis ao engine. SDK do host: .NET 10.0.302.

Os valores abaixo sao amostras deste ambiente compartilhado, nao promessa,
benchmark cientifico ou dimensionamento de producao.

## Garantias e boot

| Verificacao | Resultado |
|---|---|
| Build Release da solucao | sucesso, warnings tratados como erros |
| Testes xUnit de validacao | 9 passaram, 0 falharam, 0 ignorados |
| Schema completo em schema isolado com rollback | tabelas/indices criados e seed de 2000 imoveis validado |
| API containerizada e segunda API | readiness respondendo em 58080/58081 |
| Disputa de 200 tentativas | 1 confirmacao + 199 conflitos |
| Disputa de 1000 tentativas entre duas APIs, 64 em voo | 1 confirmacao + 999 conflitos |
| Retry de chave existente, 32 concorrentes | mesmo bookingId |
| Chave nova, 32 concorrentes | 1 criacao + 31 replays |
| Chave reutilizada com outro pedido | 409 |
| Overlaps parciais/contidos e SQL direto | rejeitados, SQL direto com 23P01 |
| Checkout/checkin adjacentes | permitidos |
| Cancelamento repetido e replay posterior | sem ressurreicao, liberacao das datas |
| 24 imoveis independentes | confirmacoes em paralelo |
| Mensagem duplicada | deliveries cresce, efeito nao duplica |
| JSON invalido | encaminhado a DLQ |
| Queda/recuperacao de PostgreSQL, Redis, RabbitMQ | script de falhas passou |

O teste de schema usa a extensao ja instalada no laboratorio e cria tabelas
num schema temporario, terminando com ROLLBACK. Nao e um teste de disaster
recovery nem substitui um teste de migrations.

## Amostras de carga da versao final

Configuracao: 32 workers, 20s de geracao, closed-loop, chamadas HTTP para API 1.
Duas APIs estavam ativas, cada uma com publisher e consumidor de eventos.
Consumer dispatch: 8 callbacks por instancia, prefetch 16.
Dataset existente cresceu entre execucoes; nao foi resetado.

| Perfil | Tentativas | Reads 200 | Bookings 201 | Conflitos 409 | Tentativas/s | Confirmacoes/s |
|---|---:|---:|---:|---:|---:|---:|
| Catalogo, read=90%, hotspot=10% | 50.582 | 45.575 | 4.529 | 478 | 2.515,35 | 225,22 |
| Disponibilidade, read=90%, hotspot=10% | 43.608 | 39.215 | 3.925 | 468 | 2.133,32 | 192,01 |
| Escrita, read=0%, hotspot=0% | 7.055 | 0 | 7.055 | 0 | 350,68 | 350,68 |

**Todas as tres amostras tiveram zero respostas inesperadas e zero overlaps.**
Os conflitos nas cargas mistas sao esperados no hotspot; nao contam como
reservas confirmadas.

| Perfil | Tempo medido (s) | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|---:|
| Catalogo misto | 20,11 | 2,86 | 93,80 | 130,91 |
| Disponibilidade mista | 20,44 | 4,83 | 83,48 | 124,58 |
| So escrita | 20,12 | 70,99 | 124,13 | 214,70 |

Latencias combinam requests do perfil, incluindo conflitos; nao sao p99 isolado
de leitura ou de sucesso de reserva. Sem repeticoes/controle de ruido, nao
concluir um percentual universal de ganho de Redis a partir destas amostras.
Disponibilidade foi consultada na nova janela do benchmark, em geral livre;
isso nao reproduz uma distribuicao real de ocupacao/busca.

Depois da carga, a projecao convergiu. Snapshot observado:

```json
{
  "confirmedBookings": 44753,
  "pendingEvents": 0,
  "processedEvents": 44933,
  "projectedActiveBookings": 44753,
  "overlappingPairs": 0,
  "totalEvents": 44933
}
```

Esse total inclui cargas/exercicios anteriores, nao so as tres linhas da tabela.
Novas execucoes alteram contagens. No instante final de uma carga a projecao
ainda podia estar atrasada; ela **nao** foi usada para autorizar reservas.
O snapshot acima foi observado apos aguardar a recuperacao do backlog.

## Problemas encontrados e corrigidos, sem esconder o primeiro resultado

### 1. Constraint correta, hotspot com operabilidade ruim

A primeira versao, com apenas EXCLUDE, manteve zero overbookings, mas uma
disputa de 1000 tentativas entre duas APIs gerou deadlocks/timeouts. Portanto
falhou a expectativa de 1x201 + 999x409 naquela execucao.

Adicionamos admissao por linha do imovel (`FOR NO KEY UPDATE`), mantendo EXCLUDE
como defesa contra SQL que ignore a admissao. Reexecutamos: o teste passou.
Trade-off: novas reservas do mesmo imovel serializam mesmo em datas disjuntas.

### 2. Outbox vazia nao significava consumidor atualizado

Com o dispatch sequencial default do cliente RabbitMQ, o HTTP gerava reservas
mais rapido que a projecao processava eventos. Uma espera de 180s nao drenou o
backlog acumulado: o teste de convergencia falhou naquele momento.

Aumentamos o dispatch para 8 callbacks por instancia, com prefetch limitado,
ACK individual e transacoes independentes. O backlog existente drenou e
deduplicacao, poison/DLQ, concorrencia e recuperacao foram novamente testados.
Mais paralelismo consome pool/CPU e pode reduzir throughput de escrita; nao
e capacidade gratuita.

### 3. Fixtures aleatorias podiam colidir com cargas anteriores

Uma carga sem hotspot intencional atingiu reservas historicas deixadas por
outras cargas, produzindo 409. Isso nao era overbooking, mas confundia a
comparacao de throughput de confirmacoes independentes.

O setup agora escolhe datas depois do maior checkout do historico e nao entra
no cronometro. Com hotspot=0, qualquer 409 e inesperado e faz a carga falhar.
Nova execucao: 7.055 tentativas e 7.055 confirmacoes. As cargas devem continuar
sequenciais: esse setup nao e um alocador de fixtures distribuido.

## Teste anterior: 100 requests em voo, pool de 100 por API

Executado em 31/10/2026, com os mesmos recursos Docker, duas APIs/ workers
ativos e 30s de geracao por perfil. Sem alterar os limites do servidor/pools.
O gerador passou a medir `peakClientRequestsInFlight`: ambos os testes
atingiram **100 operacoes HTTP pendentes no cliente**. Isso nao implica
100 queries executando no PostgreSQL ao mesmo tempo.

| Perfil | Tentativas | Reads 200 | Bookings 201 | 409 | 503 | Tentativas/s | Confirmacoes/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Catalogo, read=90%, hotspot=10% | 94.835 | 85.246 | 8.507 | 949 | 133 | 3.119,24 | 279,81 |
| Escrita, read=0%, hotspot=0% | 9.880 | 0 | 9.200 | 0 | 680 | 325,99 | 303,55 |

Na carga mista, os 503 foram 3 leituras e 130 escritas (~0,14% das tentativas).
Na carga de escrita foram 680 (~6,88%). Nao houve 429 ou erros de transporte
nessas amostras. **Ambos os testes falharam o criterio de zero respostas
inesperadas**, terminando com exit code 1. Zero overlaps nao torna esses
testes de capacidade bem-sucedidos.

| Perfil | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|
| Misto | 7,19 | 156,35 | 488,03 |
| Escrita | 288,42 | 579,08 | 1.241,88 |

O PostgreSQL informou SQLSTATE **53300**, `sorry, too many clients already`.
Inspecao confirmou `max_connections=100`. Cada API tem seu proprio pool
Npgsql com `Maximum Pool Size=100`, compartilhado com seus workers.
Dois pools de ate 100 nao respeitam automaticamente um limite global de 100.
Ainda existem conexoes de setup do simulador, administracao e reservas do banco.

Portanto **existe pooling local, mas falta dimensionar o orcamento agregado
de conexoes para esse deploy**. O limitador HTTP aceita ate 128 escritas por
instancia; ele limita requests, nao soma conexoes entre instancias.
Quando um pool tem vaga e tenta abrir uma nova conexao, o servidor pode
recusa-la porque outro pool ja consumiu o limite global.

Proximos passos de tuning: repartir o orcamento entre instancias com margem
para operacao, alinhar admissao/timeouts e medir de novo; avaliar PgBouncer
se houver necessidade. Nao aumentar `max_connections` indiscriminadamente:
mais sessoes tambem consomem memoria e podem piorar contencao. Nenhum desses
ajustes foi aplicado para maquiar o resultado deste teste.

Comandos para repetir, sequencialmente:

```powershell
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 30 --concurrency 100 --read-percent 90 --hotspot-percent 10 --read-mode catalog
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 30 --concurrency 100 --read-percent 0 --hotspot-percent 0
```

Apos a carga de escrita, esperamos o pipeline convergir e observamos
62.460 reservas confirmadas, 62.698 eventos unicos processados,
outbox pendente zero, projecao com 62.460 e **zero pares sobrepostos**.
Totais incluem historico anterior. A integridade e recuperacao permaneceram
preservadas, mas **100 requests em voo sem erros nao foi comprovado**.

## Novo teste: pool de 50 por API e concorrencias 80 / 100 / 200

A pedido do usuario, reduzimos `Maximum Pool Size` de 100 para **50 por API**
no Compose e na configuracao do host. As duas APIs foram recriadas e a
configuracao efetiva dos containers foi verificada. Mantivemos
`max_connections=100`, Timeout=5, Command Timeout=15 e os limitadores HTTP.
Nao aumentamos recursos Docker nem introduzimos retries no gerador.

Para permitir 200 workers, o limite de `--concurrency` do modo `load` passou
de 128 para 256. O modo `verify` continua limitado a 128. Isso altera somente
o gerador, nao os pools ou permissoes da API.

Cada perfil rodou por 30s, sequencialmente, com chamadas para API 1.
API 2 ficou ativa com seus workers, mas **a carga HTTP nao foi dividida entre
as duas APIs**. Antes do perfil seguinte, esperamos outbox/inbox/projecao
convergirem. A leitura foi de **catalogo cache-aside Redis**, nao disponibilidade
dinamica no banco. Dataset e cache nao foram resetados.

| Perfil | Workers / pico cliente | Requests | Reads 200 | Bookings 201 | 409 | 429 / 503 / transporte |
|---|---:|---:|---:|---:|---:|---:|
| So escrita, hotspot=0% | 80 / 80 | 12.981 | 0 | 12.981 | 0 | 0 |
| Misto, read=90%, hotspot=10% | 100 / 100 | 139.659 | 125.586 | 12.687 | 1.386 | 0 |
| So catalogo, read=100% | 200 / 200 | 551.370 | 551.370 | 0 | 0 | 0 |

**Os tres perfis passaram: zero respostas inesperadas e zero overlaps.**
Os 1.386 conflitos do perfil misto eram esperados no hotspot.
Nao houve retries automaticos para esconder erros.

| Perfil | Tempo (s) | Requests/s | Confirmacoes/s | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|---:|---:|---:|
| Escrita, 80 workers | 30,22 | 429,54 | 429,54 | 158,99 | 268,44 | 408,35 |
| Misto, 100 workers | 30,13 | 4.635,58 | 421,11 | 5,24 | 140,21 | 206,74 |
| Catalogo, 200 workers | 30,01 | 18.372,94 | 0 | 9,30 | 17,87 | 27,70 |

Pico cliente mede operacoes HTTP pendentes, incluindo espera por rede/pool,
nao queries simultaneas no PostgreSQL. Latencias do misto combinam reads,
confirmacoes e conflitos. Estas sao amostras closed-loop, nao taxas maximas
certificadas ou um SLO de producao.

### Backlog e integridade apos os testes

O sucesso HTTP nao significou projecao instantanea:

- Escrita: ao terminar, havia 421 eventos pendentes na outbox e 9.162 eventos
  ainda nao aplicados na projecao. Convergiu durante a espera seguinte.
- Misto: havia 5.239 eventos pendentes e 8.468 eventos ainda nao aplicados.
  Tambem convergiu antes do perfil de leitura.
- Leitura: nao criou reservas/eventos; outbox permaneceu vazia e projecao alinhada.

As esperas de verificacao observaram convergencia em cerca de 51,57s e 32,76s
respectivamente, **a partir do inicio de cada comando de espera**. Nao sao
medidas exatas da latencia por evento ou do tempo total desde o ultimo POST.
Continua existindo gargalo de eventos sob escrita sustentada.

Snapshot depois dos tres perfis e antes da verificacao de regressao:

```json
{
  "confirmedBookings": 88128,
  "pendingEvents": 0,
  "processedEvents": 88366,
  "projectedActiveBookings": 88128,
  "overlappingPairs": 0,
  "totalEvents": 88366
}
```

Reexecutamos tambem o teste entre duas APIs com 1000 tentativas: 1 confirmacao
+ 999 conflitos, retries/cancelamento/SQL direto/duplicatas/DLQ passaram.
Os 9 testes xUnit continuaram passando e o build Release teve sucesso.

### O que podemos concluir do ajuste?

Com pool de 50, esses cenarios nao repetiram os 503 observados com pools de
100. O pool faz requests aguardarem reutilizacao de conexoes em vez de cada
instancia tentar crescer ate 100 sessoes.

**Ainda nao existe margem garantida:** 50 + 50 consome todo o teto de 100,
sem contar outros clientes/reservas do banco. Estes testes concentram HTTP
na API 1; nao provam que ambas com pools totalmente utilizados evitariam
recusas. O perfil com 200 leituras de catalogo tambem nao prova 200 consultas
de disponibilidade no banco em paralelo.

Comparacoes com amostras anteriores nao isolam perfeitamente o efeito do pool:
workload, dataset, cache e ruido da maquina influenciam os resultados.
Para capacidade de producao, repetir testes, distribuir carga nas duas APIs,
reservar margem global e medir pool/CPU/WAL/backlog.

Comandos utilizados:

```powershell
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 30 --concurrency 80 --read-percent 0 --hotspot-percent 0
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 30 --concurrency 100 --read-percent 90 --hotspot-percent 10 --read-mode catalog
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 30 --concurrency 200 --read-percent 100 --hotspot-percent 0 --read-mode catalog
```

## Conclusao correta

Provamos o comportamento do laboratorio: exclusividade no PostgreSQL, retries
idempotentes, eventos recuperaveis e projecao convergente.

**Nao provamos 100M reads/s nem 10M bookings/s**, HA, failover com zero perda,
multi-region, pagamentos ou capacidade linear de shards.
A diferenca entre uma garantia de integridade e uma garantia de capacidade
e justamente uma das principais licoes do projeto.
