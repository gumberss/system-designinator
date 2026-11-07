# 10. Resource estimation com as premissas solicitadas

**Estimativas, nao benchmarks.** Unidades decimais: 1KB=1000 bytes,
1GB=10^9 bytes, 1TB=10^12 bytes. Payloads abaixo sao hipoteses de
dimensionamento; JSON real, Redis, indices, WAL e replicacao adicionam overhead.

## Premissas fornecidas

| Dado | Valor |
|---|---:|
| Passageiros cadastrados | 500 milhoes |
| Passageiros DAU | 20 milhoes |
| Motoristas cadastrados | 5 milhoes |
| Motoristas DAU | 3 milhoes |
| Viagens/dia | 20 milhoes |
| Intervalo de GPS de motorista online | 4 segundos |
| Pico de pedidos considerado | 10.000/s |
| Envelope de leitura considerado | 100 milhoes de posicoes entregues/s |

**DAU nao e concorrencia.** 3M motoristas que trabalham em algum momento
do dia nao significam 3M online durante as 24h.

Para calcular uma base, adicionamos hipoteses explicitas:
8 horas online por motorista DAU; viagem media de 20 minutos;
GPS de passageiro durante a viagem tambem a cada 4s.
Uma operacao real deve substituir isso por distribuicao por cidade/hora,
duracao p95/p99, turnos e picos.

## Viagens

```text
20.000.000 / 86.400 = 231,48 novas viagens/s em media global
10.000 / 231,48 = 43,2 vezes a media
20.000.000 / 10.000 = 2.000s = 33min20s
```

Se 10k/s persistisse o dia inteiro, seriam **864M viagens/dia**, nao 20M.
Com 20M/dia, 10k/s deve ser pico de duracao limitada, envelope de capacidade
ou pedidos com conversao menor que 100%. E necessario distinguir pedidos
tentados, aceitos, atribuicoes e viagens concluidas.

Lei de Little, usando 20min e a **media**:

```text
231,48 viagens/s * 1.200s = ~277.778 viagens simultaneas
```

Nao multiplicar pico de segundos por 20min sem justificar que o pico se
sustenta por esse periodo. 20M viagens/3M drivers DAU ~=6,67 viagens/driver/dia.
10k pedidos/s ainda geram varias escritas/eventos cada, nao somente 10k INSERTs.

## Ingestao de GPS

```text
3.000.000 drivers * 8h / 24h = 1.000.000 online em media
1.000.000 / 4s = 250.000 updates/s
3.000.000 / 4s = 750.000 updates/s se todos os DAU estiverem online
3.000.000 * 8 * 3.600 / 4 = 21,6 bilhoes de amostras/dia
```

750k/s e um envelope simultaneo dos DAU, nao a media diaria inferida.
5M cadastros nao sao 5M publicando GPS.

Com payload hipotetico de 200 bytes por amostra:

- media de 250k/s -> **50MB/s**, ~4,32TB/dia de payload bruto;
- envelope de 750k/s -> 150MB/s enquanto durar;
- backlog medio de 15s -> 3,75M amostras, **750MB de payload** antes de overhead.

Isso ja excede o Redis de 256MB do laboratorio: e deliberadamente impossivel
confundir a stack local com o sizing global.

GPS dos passageiros somente durante viagens medias:

```text
277.778 / 4s ~= 69.444 updates/s adicionais
20M viagens * (1.200/4) = 6 bilhoes de pontos/dia
```

Incluir passageiros com app aberto fora de viagem exige tempo medio de sessao.
Nao aplicar taxa de GPS continuamente aos 500M cadastrados.

## Ultimo valor versus indice versus trajeto

Com 1M drivers online e 15s de intervalo de indice:

```text
Hash: ~250k atualizacoes/s
Indice/checkpoint ultimo valor: ~1M / 15 = 66.667 entradas/s amortizadas
```

Reducao teorica de ~3,75x na manutencao de ultimo valor em relacao a 4s.
Nao e reducao de 3,75x em todo o sistema: stream ainda recebe cada GPS.
No laboratorio a quadtree usa rebuild completo, com custo adicional por nivel.

Um trajeto medio de 20min a cada 4s tem ~300 pontos por actor.
20M viagens -> 6B pontos de motorista/dia, ou **1,2TB/dia** a 200 bytes/ponto.
Motorista + passageiro -> 2,4TB/dia de payload de trajetos.
Nao somar cegamente isso com o stream bruto: o stream inclui os pontos que
depois viram trajetos; sao fases/copias diferentes do mesmo dado.

Se armazenasse apenas o hash a cada 15s, reduziria o trajeto a cerca de
80 pontos/viagem. A economia viria de **perder informacao**, nao de batching.
Por isso checkpoint coalesce, historico nao.

## Cadastros e memoria: ordens de grandeza

| Item | Hipotese de tamanho | Payload estimado |
|---|---|---:|
| 500M passageiros | 1KB/cadastro, fotos so como URL | 500GB |
| 5M motoristas | 2KB/cadastro | 10GB |
| 20M trips/dia | 1KB/trip | 20GB/dia (~7,3TB/ano) |
| 1M ultimas posicoes online | 256B/entrada | 256MB |
| 5M ultimas posicoes retidas | 256B/entrada | 1,28GB |

Nao sao tamanhos reais de tabela/heap/Redis. Hashes, chaves UUID, strings,
objetos JS, ponteiros de arvore, indices, copias de build e replicas exigem
fator adicional medido. Fotos/documentos precisam de object storage separado.
Motoristas offline retidos no hash nao desaparecem apenas por TTL logico.

## O que sao 100M leituras por segundo?

Aqui assumimos **posicoes individuais entregues**, nao 100M HTTP requests.
Se cada resposta entrega 20 pontos, 100M pontos/s ~=5M respostas/s antes
de considerar push e compartilhamento de snapshots.

```text
100M pontos/s * 100B = 10GB/s = 80Gbit/s, ~864TB/dia
100M pontos/s * 200B = 20GB/s = 160Gbit/s, ~1,728PB/dia
```

Se fossem 100M requisicoes HTTP/s com resposta media de 1KB:
100GB/s, 800Gbit/s e ~8,64PB/dia de payload. E outro problema.
Nenhuma dessas contas inclui headers, TLS, retransmissoes ou replicacao.

DAU tambem precisa justificar esse envelope:
20M passageiros com app aberto 2h/dia -> ~1,67M telas simultaneas em media.
Cada tela com 20 pontos a cada 4s -> ~8,33M pontos/s, nao 100M.
100M pode ser pico, incluir outras leituras ou ter frequencia/fan-out maior.
Nao tratar meta arredondada como se fosse derivada automaticamente dos DAU.

## Consequencias de arquitetura

- Regioes independentes e autoridade de matching por particao.
- Hash e overlay espacial particionados; nao delta global por request.
- Gateways assinando interesse de regiao/corrida e agrupando entregas.
- Log replicado para ingestao, ACK duravel definido e backpressure.
- Historico frio com retencao, compactacao e finalidade LGPD.
- Benchmarks por componente e ponta a ponta, incluindo banda e caudas.

Nao indicamos "N servidores exatos": faltam throughput medido por no,
distribuicao geografica, fator de replicacao, payload real, picos e SLO.
Inventar um numero de maquinas antes dessas medidas seria falsa precisao.
