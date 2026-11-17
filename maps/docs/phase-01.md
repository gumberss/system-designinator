# Fase 1: fundacao implementada

## Escopo real

HTTP, mapa, geocoding local, consentimento GPS e importacao XML OSM. Nao ha
roteamento, grafo navegavel, portais, broker, trafego, EV nem LLM nesta fase.
As vias importadas sao dados geograficos brutos, nao instrucoes de navegacao.

Python/FastAPI serve API e HTML/JS/MapLibre do mesmo origin. JavaScript fica
somente no navegador para WebGL e Geolocation; regras e persistencia em Python.
PostGIS concentra geometrias, busca textual e estado da sessao. Nao introduzimos
Redis, Kafka, OpenSearch ou processos adicionais antes das fases que os exigem.

Modulos:

- `domain.py`: entidades e interface de repositorio sem dependencias de infraestrutura.
- `osm.py`: parser XML protegido contra entidades, geometrias e proveniencia.
- `postgres.py`: PostGIS, transacoes e migracao.
- `api.py`: schemas, autenticacao, contratos, cleanup e metricas.
- `cli.py`: download explicito, importacao, migracao e execucao.
- `static`: mapa, geocoding, marcadores e GPS.

SQLAlchemy + psycopg atendem a stack aprovada. Migracao inicial SQL e ledger
transacional sob advisory lock; Alembic fica para a primeira evolucao de schema.
O migration runner desta fase aceita somente SQL simples sem funcoes contendo
`;`; nao e um parser SQL generico.

## Importacao e busca

`download` usa API oficial OSM para um recorte pequeno, timeout de 60s, ate
3 tentativas para 429/502/503/504, sem redirects, limite de 100MiB e publicacao
do arquivo somente apos validar XML. Nao sobrescreve o arquivo do usuario.

`import-osm` requer origem publica HTTPS e guarda checksum SHA-256, data,
contagens e bounding box efetivo. Importa nodes, ways e nomes em transacao;
checksum repetido reativa o snapshot existente. O snapshot ativo troca
atomicamente sob lock no banco. Historico OSM nao contem GPS do usuario.

Suporte inicial: XML `.osm`, ate 200.000 elementos e 100MiB. PBF, relations
e replicacao incremental ainda nao sao importados. Esses limites impedem
promessas de um importador global. Relations/restricoes precisam ser incorporadas
antes da fase de roteamento; nao usar estes dados como um grafo seguro.

Ways exigem todas as referencias de nodes; geometria incompleta e rejeitada.
Dados de ways podem sair do bbox pedido: a API OSM retorna seus nodes de
referencia completos. Pontos de nodes preservam a coordenada; lugares/ruas
nomeados como way recebem `ST_PointOnSurface` e `location_kind=representative_point`.
Esse ponto nao e uma entrada comercial ou GPS observado.

Geocoding usa `plainto_tsquery('simple')`, GIN e filtros PostGIS/GiST, ordenacao
deterministica e cursor offset limitado. Pesquisa nomes completos por tokens;
nao e fuzzy, reverse geocoding, interpretacao de enderecos completa nem busca
distribuida. Metadados OSM originais preservados, sem ratings ou horarios
fabricados. Exibicao de ruas limita-se a 10.000 features por recorte.

## Sessao, consentimento e retencao

POST `/api/v1/auth/session`: cria cookie opaco 256-bit HttpOnly, SameSite=Strict,
24h, com digest no banco e CSRF separado. Origin deve estar explicitamente
permitido. Cookies Secure configuraveis para HTTPS. Nao e cadastro/OIDC;
sessao privada de um laboratorio local e nao identidade de producao.

POST `/api/v1/location/tracking` exige `{tracking_consent:true}`, cookie,
Origin e `X-CSRF-Token`. Uma sessao de rastreamento por owner; cada ativacao
rotaciona seu ID e apaga a posicao anterior, evitando sequencias antigas apos
refresh. Outras abas do mesmo browser compartilham owner e nao podem ter
rastreadores independentes nesta fase.

POST `/api/v1/location` aceita somente sessao ativa do owner. Validacoes:
coordenadas finitas WGS84, timestamp timezone-aware de -60s a +15s, sequencia
nao negativa, accuracy/speed nao negativos e heading [0,360). Retorna 202 apos
commit; duplicata identica do ultimo event_id e idempotente, payload diferente
com o mesmo ID retorna 409, evento fora de ordem retorna 409. Nao ha ledger
indefinido de eventos nem exactly-once. Evento repetido depois de outra
observacao e rejeitado pela sequencia, nao reprocessado.

GET `/api/v1/location/current`: ultima observacao e freshness; 404 quando
ausente/expirada. DELETE no mesmo endpoint revoga rastreamento e apaga a posicao
por cascade, idempotentemente. Operacoes usam transacoes e row locks, nao locks
Python, para preservar isolamento entre replicas.

Cliente pede permissao GPS antes de consentir no servidor. Exibe coordenadas
localmente; envia com deslocamento >=10m ou heartbeat de 30s, minimo 1s entre
envios, somente um request pendente e coalescing da observacao mais recente.
Dados de GPS nunca sao enviados a LLM, tiles ou analise de trafego nesta fase.
Tiles externos podem revelar a area visualizada ao provedor; configure somente
se aceitar essa politica. O CDN recebe requests de biblioteca, nao payload GPS.

Somente ultima observacao e armazenada, TTL 5min; cleanup no maximo a cada
60s remove expirados e sessoes de 24h. TTL logico vale mesmo antes do cleanup
fisico. Se banco/API indisponivel, limpeza fisica pode atrasar: alertas de falha
de retencao sao logados, nunca ocultados. Backups e criptografia at-rest dependem
da operacao externa e ainda nao estao garantidos.

## Endpoints desta fase

| Endpoint | Status principal |
|---|---|
| GET /health/live | 200 processo vivo |
| GET /health/ready | 200 schema/PostGIS pronto; 503 DB/schema indisponivel |
| GET /metrics | 200 counters HTTP e soma de duracoes |
| GET /api/v1/config | 200 capacidades e estilo opcional |
| POST /api/v1/auth/session | 201 novo cookie, 200 existente |
| POST /api/v1/location/tracking | 201 nova sessao com consentimento |
| POST /api/v1/location | 202 aceite/deduplicacao |
| GET /api/v1/location/current | 200 posicao com freshness; 404 ausente |
| DELETE /api/v1/location/current | 200 revogado/apagado |
| GET /api/v1/geocoding/search?q=...&bbox=...&limit=20&cursor=0 | 200 items/next_cursor; 503 sem dataset importado |
| GET /api/v1/datasets/current | 200 metadata/geometrias; 404 sem importacao |

Erros Problem Details, 401 sem sessao, 403 Origin/CSRF/consentimento, 409 conflito,
422 schema/limites, 503 DB. OpenAPI em `/docs`. Payloads de erro nao repetem
coordenadas enviadas, parametros SQL ou credenciais. Logs de acesso Uvicorn
desativados na CLI para nao registrar query strings; logger JSON usa template
de rota, request_id, status e duracao, sem payload.

## Resultados verificados em 2026-11-15

Host Windows, Python 3.14; ambiente virtual exclusivo `maps/.venv`.

| Verificacao | Resultado |
|---|---|
| Instalacao editable `.[dev]` e CLI `--help` | Passou |
| pytest | 32 passed, 1 skipped; warning de deprecacao Starlette/httpx |
| mypy strict | Sem erros |
| Ruff check | Passou |
| Pylance sintaxe de API/CLI/PostGIS/parser | Sem erros |
| Docker Compose moderno `config --quiet` | Passou, sem iniciar containers |
| Download HTTPS oficial OSM | 200; XML validado |
| Dataset Sao Paulo | 2.802.200 bytes, 7.008 nodes, 793 ways, 325 nomes |
| Browser: carregamento MapLibre, rua local e marcador de busca | Passou com fixture sintetica identificada em `tests/web_fixture.py` |
| Browser: GPS simulado, POST 202, desativar/clearWatch e GET 404 | Passou com fixture; nao e medicao de GPS fisico |
| Browser: permissao simulada negada | Passou; zero requests de consentimento |
| PostGIS/Compose real | Bloqueado; nao executado |

Checksum e origem do XML real em `data/sources.json`. O arquivo XML esta
localmente em `data/sao-paulo.osm`, ignorado pelo Git. Nao foi importado no
banco neste host. O download foi testado, nao somente substituido por mock.

O teste browser revelou um erro de inicializacao do marcador GPS: `addTo`
era chamado antes de definir coordenadas. Corrigido para `setLngLat` antes
de `addTo` e cenario reexecutado com sucesso. A ferramenta browser nao suporta
`Browser.grantPermissions`; os cenarios GPS usaram uma Geolocation controlada
no browser. Permissao real do sistema e GPS fisico continuam nao validados.

Docker do PATH: 19.03.12. Docker Desktop encontrado: 27.2.0, Compose 2.29.2.
Ambos falham ao conectar ao named pipe `dockerDesktopLinuxEngine`.
`wsl --status` nao terminou e foi interrompido. Diretorio PostgreSQL 16
encontrado, mas sem binarios ou extensao PostGIS. Nao iniciamos/reconfiguramos
servicos compartilhados, nem usamos bancos de outros projetos.

O teste PostGIS esta disponivel e explicitamente skipped sem URL de banco
isolado. Esse bloqueio impede declarar a Fase 1 completamente validada.

## Limites e proximo marco

O parser e APIs locais foram testados; nao ha throughput servidor medido,
prova de HA, integracao multi-instance real, SLO validado ou escala mundial.
As metricas iniciais nao substituem tracing distribuido nem histogramas p95.
Importacao em memoria e geometrias em uma resposta atendem somente o recorte
regional; tiles vetoriais, PBF streaming e artefatos de grafo entram depois.

Antes de avanzar: restaurar Docker Linux/PostGIS, executar teste real em banco
isolado, importar XML real e validar o mapa com o banco. Depois, mediante
aprovacao, Fase 2 entrega grafo dirigido, restricoes, portais, A*/Dijkstra,
rotas e instrucoes verificadas contra oraculo.
