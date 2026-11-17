# 12. Resultados verificados da versao de servicos

Este capitulo registra execucoes locais, nao extrapolacoes de capacidade.
O [capitulo 8](08-resultados-verificados.md) e o historico da versao monolitica.
Nao provisionamos recursos de cloud.

## Ambiente

Windows, Node 24.3.0, npm 11.16.0, Docker Desktop Linux engine 27.2.0.
Docker reportou 5 CPUs e ~4,81GiB de memoria.
PostgreSQL/PostGIS 17-3.5, Redis 7.4 e Nginx 1.28 via Compose.
Outras cargas da maquina nao sao controladas: tempos sao observacoes,
nao benchmarks reproduziveis de throughput.

## Build e testes

| Verificacao | Resultado |
|---|---|
| npm test | 11 testes passaram: 6 dominio + 5 quadtree |
| npm run test:integration | 18 cenarios + teste agregado = 19 passaram; 0 falhas |
| Sobrecarga no ultimo burst | integration_overload_responses=0 nessa execucao |
| Duracao da ultima integracao | ~15,1s no runner |
| Build da imagem compartilhada | TypeScript compilado; imagem gerada |
| nginx -t | configuracao valida com resolucao DNS dinamica |
| Readiness dos dois gateways | ready=true em 58100 e 58101 |
| Processos aplicacionais | todos healthy apos recriacao |
| Schema limpo | 0 colunas GPS em `drivers`; `schema_migrations` e `event_clock` ausentes; 4 checkpoints, 0 viagens/rotas |

O contador de sobrecarga registra respostas 503, nao pedidos perdidos.
Outras execucoes e ambientes podem apresentar retries. O teste possui budget
finito e nao ignora erros diferentes de 503.

### Cenarios de integracao exercitados

1. Schema limpo, cadastro separado dos checkpoints de localizacao.
2. Hash aceita GPS antes do checkpoint SQL; sequencia, prazo e identidade.
3. Delta encontra motorista que cruzou fronteira; filtros de frescura/online.
4. Delta >2000 e hash de 10k entidades falham sem truncar/confirmar indevidamente.
5. 40 pedidos entre duas request APIs disputam um carro: 1 assigned, 39 searching.
6. 24 retries com mesma chave retornam um ID; conflitos e replay terminal.
7. UNIQUE contra SQL direto; versao/proximidade e liberacao ao concluir.
8. Flush preserva pontos de ambos os actors apos conclusao; replay deduplicado.
9. Falha SQL deixa lote pending e retoma depois da recuperacao.
10. Writer recupera pending ocioso de outro consumidor com XAUTOCLAIM.
11. Falha de evento reverte pedido; contadores independentes por corrida.
12. Mapa indisponivel devolve 503 mas preserva intencao; retry usa mesma corrida.
13. Dispatcher atribui searching recuperado e expira prazo vencido.
14. SSE de estado retoma cursor por outra instancia; SSE GPS transmite ultimo valor.
15. Cursor antigo adiantado reinicia replay; evento legado location preserva tipo.
16. Desconexao durante subscribe libera assinatura tardia de estado e GPS.
17. Redis desconectado devolve falha explicita, sem GPS SQL como fallback.
18. Contratos invalidos, corpo >64KB e pool SQL saturado.

A suite utiliza schema PostgreSQL e prefixo Redis exclusivos por execucao.
Os dados da demonstracao nao foram usados como fixtures mutaveis dos testes.
Falhas induzidas geram logs esperados; nao devem ser confundidos com teste aprovado
silenciosamente apesar de erro.

## Jornada visual e persistencia

No browser, foi exercitada a corrida
`bfa864eb-5ff3-4631-b3e4-9a9d10e887e1`:

- Passageira Ana; sistema escolheu Elisa.
- Distancia no matching ~577m; preco fixo simulado R$8,40.
- searching -> assigned -> arrived -> in_progress -> completed.
- Versao final 5, com 5 eventos de estado e cursores locais 1 a 5.
- Driver GPS recebido via SSE e marcador do passageiro presente.
- Botao de inicio acionado apos arrived.
- Consulta de trajeto no painel mostrou **114 pontos de motorista e 114 de
  passageiro**, conferidos tambem diretamente no PostgreSQL.
- Endpoint de mapa retornou 4 pontos, 1 folha e refreshMs=15000.

O trajeto inclui o tempo de espera em arrived durante a validacao manual.
Portanto o numero de pontos nao e benchmark de duracao de uma viagem normal.
O screenshot confirma o painel, mas movimento nao e inferido de screenshot:
foi observado por sequencias/posicoes e transicoes ao longo da jornada.

A jornada visual e as 114 amostras por actor foram verificadas antes da
recriacao limpa do volume descrita abaixo. A recriacao interrompeu
temporariamente HTTP/SSE: houve 502/504 e reconexao, nao failover sem
indisponibilidade. O laboratorio nao tem deploy rolling sem downtime.

O browser integrado registrou ERR_ABORTED para alguns POSTs de GPS com
resposta 204; os access logs do gateway confirmaram 204 para esses pedidos.
Nao usamos esse aviso isolado como evidencia de perda de GPS.

## Problemas encontrados e corrigidos

- Suite antiga acoplada ao monolito foi substituida por chamadas entre servicos.
- Recuperacao de Redis no teste passou a aguardar desconexao real.
- Cursor maior que o cursor atual da corrida e recuperado com replay.
- Gateway usa DNS Docker reavaliado, em vez de IP imutavel de container.
- Trajeto nao depende de a corrida ainda estar ativa na hora do flush.
- Limite de SSE reserva a vaga antes de subscribe; desconexao nesse intervalo
  nao deixa assinatura ou contador vazando.

## O que nao foi comprovado

Nao fizemos teste de 10k pedidos/s, 100M leituras/s, 500M passageiros,
3M drivers concorrentes, failover regional, restore de desastre ou RPO zero.
Nao validamos rede movel real, autorizacao, fraude de GPS, pagamento,
trafego/ETA ou roteamento rodoviario.

Os [calculos de recursos](10-estimativa-de-recursos.md) e a
[arquitetura global proposta](06-escala-e-cloud.md) sao raciocinio de projeto.
O codigo local demonstra os protocolos e seus limites, nao certifica aquela escala.

## Recriar sem dados de versao anterior

Por decisao de escopo, nao existe fluxo de migration/upgrade no repositorio.
Depois de atualizar o Compose para usar exclusivamente `001-schema.sql`, os
volumes locais `ride-hailing_ride-data` e `ride-hailing_redis-data` foram
removidos e recriados para iniciar somente com as tabelas e fixtures atuais.
Logo, as viagens de teste mencionadas acima sao evidencias da execucao anterior,
nao dados existentes no banco atual. O Compose foi iniciado novamente; os dois
gateways responderam `ready=true` e os servicos estavam saudaveis na verificacao.
Veja [operacao e reset local](11-migracao-e-operacao.md).
