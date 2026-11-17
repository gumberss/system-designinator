# Fase 2: grafo regional e rotas

## Entrega

Grafo dirigido de roads OSM com modos car/bicycle/walking, distancias geodesicas,
velocidades baseline por tipo/maxspeed/surface, acesso, one-way e restricoes de
conversao. A*/Dijkstra, segmentacao geografica, portais, atalhos locais com cache,
busca hierarquica, snapping fracionario, alternativas, geometria e instrucoes.
Formulario no mapa, API privada de rotas com persistencia e CLI offline.

Sem trafego real, navegacao GPS turn-by-turn, transporte publico, EV ou LLM.
Seguir uma rota experimental nao e uma garantia de seguranca na via.

## Grafo, portais e algoritmos

Cada par de nodes consecutivos de uma way gera arestas por sentido permitido
para cada modo. O grafo mantem a identidade OSM da way e o sentido original.
Barreiras, acesso private/destination/customer/delivery e condicionais nao
suportados sao excluidos conservativamente. Access genericamente yes nao libera
carros em footways ou pedestres em motorways. Bicycle em steps exige permissao
explicita. Velocidades sao estimativas, nao observacoes reais.

Grade inicial de 0,01 grau, nao usada como unidade de distancia. Arestas que
atravessam celulas sao divididas em vertices virtuais nas fronteiras, mesmo sem
uma intersecao original. Portais sao vertices com arestas em multiplas celulas;
conexoes preservam IDs originais nas intersecoes. `Graph.segments` identifica
vertices internos/portais e `Edge.segment` identifica o subgrafo local.

Estado da busca e `(vertex, incoming_edge)`, necessario para restricoes de
conversao. A* usa distancia geodesica como lower bound para shortest, e distancia
dividida pela velocidade maxima do grafo para fastest. Dijkstra tem heuristica
zero e e o oraculo regional dos testes.

O algoritmo hierarquico calcula caminhos locais ate os portais ou destino via
Dijkstra edge-based, publica esses caminhos como transicoes no overlay e usa
A* entre portais. Reconstrucao concatena as arestas detalhadas. O estado de
entrada/saida preserva restricoes entre segmentos, nao apenas custos por vertice.
Nenhum bounding box origem/destino corta as alternativas: a busca pode visitar
qualquer segmento do recorte importado.

**Decisao incremental:** atalhos sao precomputados sob demanda no primeiro
acesso e cacheados no objeto Graph, em vez de materializar todos os pares de
portais antes de servir. A chave inclui estado, modo, estrategia, destino,
preferencias e arestas excluidas. Cache limitado a 4.096 entradas; `index()`
invalida apos mudancas de geometria. O cache vive no grafo da consulta, nao
e uma promessa de cache distribuido ou de grafo mundial.

Snapshots independentes de segmentos em storage, carregamento lazy de shards
e overlay inter-servidor ainda sao evolucao. Nesta fase, o grafo regional cabe
em memoria e e reconstruido por consulta; segmentacao e hierarquia estao
implementadas, mas nao a distribuicao de artefatos prevista na arquitetura.
Esse limite esta explicito para nao chamar um laboratorio regional de global.

Trade-offs: Dijkstra simplifica a prova de custo minimo mas expande mais estados;
A* reduz trabalho quando a heuristica e informativa; hierarquia paga buscas
locais e cache de caminhos, podendo ser pior em grafos pequenos. Bidirectional
Dijkstra exigiria tratamento simetrico de restricoes; CH/CCH e armazenamento
compacto/nativo ficam para medicao de redes maiores. Nao ha promessa de que
hierarquia sempre seja mais rapida.

## Snapping, restricoes e alternativas

Snapping procura a aresta permitida mais proxima dentro de 100m, projeta o ponto
em coordenadas locais com correcao de longitude e calcula erro geodesico.
Insere vertice virtual e divide ambos os sentidos quando existem. Origem/destino
na mesma aresta one-way conservam o sentido e a distancia parcial; nao basta
escolher a intersecao mais proxima.

Geometria inicia/termina nos pontos snapped; deslocamento ate a via nao e
incluido na distancia/ETA. `source_snap_m` e `destination_snap_m` informam esse
erro. Nao faz lane-level snapping nem promete resolver ambiguidades entre vias
paralelas muito proximas.

Relations agora sao preservadas na importacao XML. Suporte deterministico para
no/only turn com um from-way, um via-node e um to-way, por modo e except.
Via-way, multiplos membros, restriction conditional e valores desconhecidos
excluem as ways afetadas com warning. Isso pode impedir uma rota real valida,
mas nao inventa permissao. O arquivo OSM pequeno nao garante cobertura integral
de restricoes externas ao recorte.

Alternativas: busca limitada de desvios, excluindo uma aresta da rota primaria
por vez (ate 30 candidatos), aceitando ate 2 rotas distintas com overlap <=85%
do comprimento da alternativa. Ordena os candidatos aceitos por custo.
**Nao e k-shortest exato nem garante todas as alternativas globais.**
Quantidade insuficiente gera warning explicito, nao respostas duplicadas.

Busca tem limite de 200.000 estados e 3s compartilhados por rota/alternativas.
API usa pool de 2 processos, fora do event loop, com admissao limitada a 2 jobs.
Fila cheia retorna 429. Timeout da espera retorna 503; a vaga permanece ocupada
ate o job efetivamente terminar. Falha fatal do pool retorna 503 e exige restart.
Importacao/build/snapping e overhead de processos sao adicionais: deadline de
3s do pathfinding nao prova SLO HTTP de 3s.

## API e persistencia

POST `/api/v1/routes`: cookie, Origin permitido e `X-CSRF-Token`, retorna 201.

```json
{
  "source": {"latitude": -23.5596178, "longitude": -46.6562147},
  "destination": {"latitude": -23.5538367, "longitude": -46.6481601},
  "mode": "car",
  "strategy": "fastest",
  "alternatives": 1,
  "preferences": {"avoid_tolls": false, "avoid_motorways": false}
}
```

Endpoints tambem aceitam nome textual inequivoco. Se houver zero/multiplos
candidatos, retornar 400 `ambiguous_geocoding`, exigindo coordenadas; nao
selecionar silenciosamente o primeiro resultado.

Resposta inclui route_id, dataset_id, graph_version (checksum OSM), mode,
strategy, algorithm, traffic_source=baseline, geometry GeoJSON, distance_m,
eta_s, edge_ids, instructions, alternatives, warnings, erros de snapping,
numero de segmentos/portais, estados expandidos e atalhos resolvidos.
Instrucoes basicas: depart, continue, turn_left, turn_right e arrive.
Ainda nao inclui anuncios de faixas, saida numerada de rotatoria ou voz.

GET `/api/v1/routes/{route_id}` retorna a rota somente ao owner, ate 24h; 404
para ausente/inacessivel/expirada. Rota salva conserva snapshot e ETA calculada,
nao e atualizada com um dataset publicado depois. Cleanup remove rotas
expiradas, inclusive por cascade ao expirar a sessao do browser.

Validacao finita/WGS84 e schemas extra=forbid. 422 para modo/opcoes invalidos,
fora da cobertura ou caminho inviavel; 503 sem grafo/DB ou orcamento excedido.
Sem fallback para linha reta ou dados simulados.

Migracao 002 adiciona osm_relations, saved_routes e routing_ready. Migra dados
da Fase 1 sem apaga-los. Datasets antigos permanecem routing_ready=false ate
reimportar o XML, inclusive checksum repetido; evitando ignorar restricoes que
a Fase 1 nao armazenava. Migracoes e publicacao usam locks/transacoes no banco.
Validacao real dessas transacoes continua bloqueada pelo Docker do host.

## Execucao offline reproduzivel

Na pasta maps, com ambiente da Fase 1:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\mapinator.exe route-osm .\data\sao-paulo.osm --source=-23.5596178,-46.6562147 --destination=-23.5538367,-46.6481601 --mode car --strategy fastest
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
& .\.venv\Scripts\python.exe -m ruff check .
```

O comando offline faz calculo real, nao um fallback da API, e nao salva dados
privados em banco. Contratos HTTP/UI continuam dependentes de PostGIS real.
O XML real local e ignorado pelo Git, com fonte/checksum em data/sources.json.

## Resultados medidos

Windows, Python 3.14, 2026-11-15. Sem teste de carga sustentado.

- Suite: **52 passed, 1 skipped**, com warning Starlette/httpx preexistente.
- Mypy strict e Ruff sem erros.
- Testes: A*/Dijkstra/hierarquia, grades sinteticas 4x4/10x10/25x25, restricoes
  entre segmentos, acesso, one-way, snap parcial, rotas fora do bbox inicial,
  cache/invalidation, alternativas, limites, ownership e expiracao.
- API com pool real de processos: erro de caminho inviavel serializado sem
  quebrar o worker, seguido de nova rota bem-sucedida.
- Browser com fixture sintetica: POST 201, linha no mapa, ETA e instrucoes;
  falha 422 limpa a rota antiga e instrucoes. Nao e uma integracao PostGIS.
- OSM real: 155 relations, 7.044 vertices (incluindo nodes nao roteaveis e
  virtuais), 3.211 arestas dirigidas, 7 segmentos e 36 portais.
- Parser aproximadamente 0,220s; build aproximadamente 0,036s numa execucao.
- 10 pares aleatorios de nodes car, seed 42: **2 viaveis, 8 sem rota**;
  todos os algoritmos concordaram na viabilidade e no custo dos caminhos.
  O recorte pequeno e exclusoes conservadoras limitam a conectividade.
- Nos dois casos viaveis: hierarchical aproximadamente 0,57ms e 1,40ms,
  A* 0,57ms e 1,11ms, Dijkstra 0,53ms e 1,69ms, sem parse/build/processo/DB.
- CLI real completa em processo novo: aproximadamente 1,65s, **1.822,18m,
  ETA baseline 171,61s, 84 arestas**. Isso nao mede acuracia de ETA real.

Essas amostras minusculas nao fundamentam RPS sustentado, p95 de producao,
dimensionamento de servidores ou escala global. A comparacao com Dijkstra
prova custo no mesmo grafo, nao a completude dos dados/semantica OSM.

Docker 27.2.0 ainda nao conecta a dockerDesktopLinuxEngine. Integracao real
PostGIS, migracao de banco existente e UI com banco permanecem nao verificadas.
O teste real foi ampliado e continua explicitamente skipped sem URL isolada.
Nenhum banco de outro projeto ou servico compartilhado foi alterado.

Proximo marco mediante aprovacao: trafego com ingestion/outbox, pub-sub,
agregacao e pesos dinamicos; primeiro validar as migracoes reais quando houver
Docker Linux/PostGIS funcional.
