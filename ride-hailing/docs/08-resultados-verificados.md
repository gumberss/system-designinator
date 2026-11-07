# 8. Resultados realmente verificados

> **Historico da primeira versao monolitica.** Estes resultados descrevem o
> codigo anterior a separacao de servicos. O runtime atual nao usa contador
> global nem GPS sincrono no PostgreSQL. Consulte os
> [resultados da versao de servicos](12-resultados-servicos.md) e o README atual
> para comandos, dependencias e garantias vigentes. O historico e mantido para
> nao apagar evidencias e problemas encontrados anteriormente.

Execucao local em **02/11/2026**, Windows, Node **24.3.0**, npm **11.16.0**,
Docker Engine **27.2.0**, Linux containers.
Engine com **5 CPUs** e **5.161.676.800 bytes de RAM** (~4,81 GiB).
Ambiente compartilhado: estes resultados nao sao benchmark controlado.

## Evidencias finais

| Verificacao | Resultado observado |
|---|---|
| `npm run build` | compilacao TypeScript estrita sem erros |
| `npm test` | 6 testes unitarios passaram |
| `npm run test:integration` | 13 cenarios passaram; runner conta 14 com o teste agregador |
| Disputa de 40 pedidos logicos, duas APIs, um motorista | 1 assigned, 39 searching, zero atribuicoes duplicadas |
| 24 retries logicos entre APIs | 1 criacao, 23 replays, um unico trip ID |
| Mesmo passageiro com outra chave ativa | 409 |
| Mesma chave com payload diferente | 409 |
| SQL direto tentando duplicar motorista | rejeitado por UNIQUE, SQLSTATE 23505 |
| Busca geografica | categoria, raio, offline, ordem por distancia e frescura validados |
| GPS repetido/atrasado | 409; timestamp fora da janela: 400 |
| Chegada/conclusao longe do ponto | 409; proximidade com GPS recente habilita transicao |
| Versao obsoleta | 409, sem regressao de estado |
| Conclusao/cancelamento repetido | motorista liberado; sem nova versao/evento para repeticao do estado atual |
| Retry de pedido concluido | mesmo ID, estado completed |
| Searching e dispatcher | recupera oferta ou muda para expired |
| Falha antes do evento | rollback de trip, request e busy; nenhum evento parcial |
| Cursor transacional sob duas transacoes | segundo cursor espera commit do primeiro; replay nao pula evento |
| Pool deliberadamente saturado | HTTP 503 explicito |
| SSE na segunda API com Last-Event-ID | apenas evento posterior, sem replay do cursor excluido |
| Contratos HTTP | 400 para JSON/UUID/coordenadas/chave invalidos; 413 para body >8kb; 404 para rota ausente |
| `docker compose config --quiet` | configuracao valida |
| Duas APIs containerizadas | readiness true em 58100 e 58101 |
| `node --check public\app.js` | sintaxe valida |
| Diagnosticos do editor | nenhum erro reportado no novo laboratorio |
| Laboratorio anterior | 44 arquivos rastreados conferidos contra o indice Git, sem diferencas |
| Links de documentacao | 10 arquivos Markdown conferidos, nenhum link local quebrado |
| GPS apos parar simulador | zero motoristas standard disponiveis depois de 16s |
| Estado persistido apos exercicios | 1 cancelled, 1 completed, zero motoristas duplicados e zero divergencias de busy |

O burst usa ate 5 tentativas para 503, com backoff e **mesma chave**.
Na execucao final houve **7 respostas 503** nos bursts antes de convergir.
O runner informou `integration_overload_responses=7` e terminou com exit code 0.
Isso prova tratamento e integridade de **pedidos logicos sob retry**, nao
40 respostas iniciais 201 sem sobrecarga. Tempo total do runner ~12,2s nesta
amostra, sem equivaler a throughput sustentado.

## Processos realmente distintos

Os testes automatizados abrem duas apps HTTP com pool compartilhado no host.
Separadamente, subimos o perfil multi-instance do Compose, com dois containers
e pools independentes, e enviamos 24 chamadas concorrentes alternando portas.

Resultado desse exercicio HTTP:

```json
{
  "created": 1,
  "replay": 23,
  "uniqueTripIds": 1,
  "terminalReplay": "cancelled"
}
```

Uma fixture de passageiro foi criada para esse exercicio. Somente a corrida
criada por ele foi cancelada; seu historico ficou no banco local.
As fixtures de todos os testes automatizados foram isoladas em schemas
aleatorios e removidas ao final, nao no schema do painel.

## Fluxo visual no navegador

Painel aberto e exercitado no navegador integrado:

1. Busca mostrou motoristas standard disponiveis.
2. Pedido escolheu Diego e exibiu a distancia no momento do matching.
3. SSE exibiu GPS sequenciado e marcador do motorista escolhido.
4. Simulador levou o motorista ao embarque e mudou para arrived.
5. Botao de inicio habilitado; comando mudou para in_progress.
6. Movimento continuou ao destino; estado final **completed, versao 5**.
7. Botoes de iniciar/cancelar ficaram desabilitados e motorista voltou a oferta.
8. Preparar nova viagem limpou a selecao.

A corrida visual ficou persistida como completed. A tela nao apresentou erro.
O mapa observado e SVG georreferenciado simplificado, nao ruas reais.
O simulador foi parado ao final da verificacao; banco e duas APIs locais
permaneceram ativos. Para voltar a ver oferta/movimento, execute `npm run simulate`.

## Resultados iniciais ruins e ajustes

Uma rodada anterior de 40 requests, concorrente com build Docker, esgotou a
espera de 2s do pool. O codigo original classificava esse erro sem SQLSTATE
como 500. A garantia de exclusividade nao bastava para considerar a rodada boa:
o teste que esperava todos 201 falhou.

Corrigimos a classificacao dos erros conhecidos de pool/conexao para 503 e
adicionamos um teste que satura o pool deliberadamente. Nao aumentamos
arbitrariamente o pool nem os timeouts.

Outra rodada com simulador e painel ativos tambem recebeu 503. O teste de
integridade passou a modelar o contrato real de retry de indisponibilidade:
budget finito, backoff, chave preservada e contagem explicita de sobrecarga.
A assercao final continua exigindo exatamente 1 motorista atribuido e
39 searching. Erros diferentes de 503 continuam fazendo o teste falhar.

**Nao confundir isso com solucionar capacidade.** Para servir todos os requests
iniciais sem 503 seriam necessarios admission control/tuning e nova medicao,
considerando o contador global, IO do host e concorrencia agregada.

## O que nao foi verificado

- Throughput sustentado, percentis de latencia ou SLO comercial.
- Milhares de streams, grandes trajetos ou retencao de semanas.
- Failover PostgreSQL, backup/restore, perda de regiao e particionamento de rede.
- Experimento manual de parar o banco descrito no capitulo 7.
- Redis, Kafka, rotas rodoviarias, aceite com lease, pagamentos e cloud.
- Seguranca de producao ou compatibilidade de uma frota real de dispositivos.

Esses itens sao extensoes/propostas, nao garantias implicitas.

## Repetir

```powershell
Set-Location .\ride-hailing
npm ci
docker compose up -d postgres
npm test
npm run test:integration
docker compose --profile multi-instance up -d --build
# Em outro terminal:
npm run simulate
# Abrir http://127.0.0.1:58100
```

O banco local preserva as duas corridas dos exercicios HTTP/visual. Inicializacao
nao e reset automatico. Novas execucoes terao contadores, posicoes e tempos
diferentes; nao use os IDs ou medidas pontuais acima como fixtures universais.
