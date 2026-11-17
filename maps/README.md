# Mapinator

Plataforma de mapas, rotas, navegacao, transito, planejamento EV e descoberta
conversacional de lugares, com backend em Python.

**Estado: Fases 1 a 6 implementadas e fase 7 com instrumentacao/benchmark
regional. Testes locais e browser com fixtures passaram; integracoes reais
PostGIS/Kafka/Redis/modelo e validacao de escala/HA permanecem pendentes.
Docker Linux indisponivel e nenhum LLM local configurado neste host.
Nao esta pronto para producao.**

## Entregue

- Backend Python/FastAPI, schemas Pydantic e OpenAPI.
- Mapa MapLibre com geometrias OSM locais, busca e marcadores.
- Geocoding por nomes no extrato importado, filtros bbox e paginacao.
- Localizacao com consentimento, autenticacao por sessao de browser, CSRF,
  atualizacoes limitadas, TTL de 5 minutos e exclusao ao desativar.
- Importacao XML OSM em transacao, proveniencia, checksum e snapshot ativo.
- Health checks, logs estruturados sem payload GPS e metricas HTTP basicas.
- Compose isolado, testes, mypy e Ruff.
- Grafo dirigido por modo, A*/Dijkstra e roteamento hierarquico por portais.
- Rotas mais rapidas/curtas, alternativas limitadas, ETA baseline e instrucoes.
- APIs privadas de rotas e calculo offline com XML real, sem banco.
- Contribuicao de transito por opt-in separado de carro, outbox transacional,
  Redpanda/Kafka, workers e DLQ sem GPS.
- Map matching dirigido, agregacao robusta, historico por horario e pesos/ETA
  dinamicos de carro com fallback explicito.
- Consulta manual de transito por segmento no mapa, com fonte e versao na API.
- Navegacao HTTP/WS, progresso, ETA restante, reroute com preferencias e Redis TTL/CAS.
- Planejamento EV com reserva por perna, multiplas cargas, intervalos de energia
  e estacoes derivadas de OSM; disponibilidade desconhecida.
- Busca deterministica de POIs, horarios verificados, conversa local Ollama
  com criterios tipados e contexto privado com revisao/TTL.
- Benchmark concorrente real por XML OSM, percentis, erros, CPU/RSS e limites.

Capacidade implementada nao significa dependencia disponivel.
Sem modelo local, conversa retorna erro; sem Redis, navegacao nao inicia.
Busca/rota comuns continuam sem esses servicos opcionais.

O desenvolvimento sera incremental, conforme o briefing:

1. [Arquitetura, Mermaid, dados, contratos, ADRs, marcos e riscos](docs/architecture.md).
2. [Fase 1: desenho, APIs, resultados verificados e limites](docs/phase-01.md).
3. [Fase 2: rotas, contratos, resultados e limitacoes](docs/phase-02.md).
4. [Fase 3: pipeline, consentimento, contratos, testes e limites](docs/phase-03.md).
5. [Fase 4: navegacao, contratos, estado, testes e limites](docs/phase-04.md).
6. [Fase 5: EV, energia, carregadores, reserva e intervalos](docs/phase-05.md).
7. [Fase 6: busca/conversa local, criterios, horarios e privacidade](docs/phase-06.md).
8. [Fase 7: medicoes reais regionais e aceite de escala ainda pendente](docs/phase-07.md).
9. Validar dependencias reais e os criterios pendentes antes de producao.

Todo o projeto ficara nesta pasta, com configuracoes, portas e volumes isolados
dos outros laboratorios. Nao sera criado nenhum recurso cloud.

## Instalar e testar no PowerShell

Python 3.12+; validacao deste host feita com Python 3.14. Nao usar os ambientes
virtuais dos outros projetos.

```powershell
Set-Location H:\Github\system-designinator\maps
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m mypy
& .\.venv\Scripts\python.exe -m ruff check .
```

Neste host houve falta de espaco em H:, resolvida removendo somente bytecode
gerado da propria `.venv`; nenhum dado de usuario foi removido. Mantenha espaco
livre antes de instalar/importar. `$env:PYTHONDONTWRITEBYTECODE='1'` evita
regenerar esse cache; testes continuam executando normalmente.

Os testes de integracao real sao skipped sem os endpoints exclusivos indicados
na secao de integracao e no [relatorio da Fase 3](docs/phase-03.md).
Testes unitarios usam dados sinteticos identificados e um adaptador exclusivo
de testes; a aplicacao real nunca faz fallback para memoria.

## Calcular uma rota real sem Docker

A Fase 2 tambem funciona diretamente sobre o XML local, sem fingir que o banco
esta disponivel. O comando nao cria uma sessao HTTP nem salva a rota no banco:

```powershell
& .\.venv\Scripts\mapinator.exe route-osm .\data\sao-paulo.osm --source=-23.5596178,-46.6562147 --destination=-23.5538367,-46.6481601 --mode car --strategy fastest
```

Exemplo validado neste snapshot: aproximadamente 1,82 km e ETA baseline de
2,86 minutos. Retorna JSON com geometria, IDs de arestas, instrucoes, avisos,
versao do grafo e metadados de segmentacao. Nao e navegacao certificada.

Opcoes: `--mode car|bicycle|walking`, `--strategy fastest|shortest`,
`--alternatives 0|1|2`, `--avoid-tolls`, `--avoid-motorways`.

## Executar com Docker Compose

Requer Docker Desktop com engine Linux e Compose v2 funcionando. Neste host,
o `docker` do PATH e antigo; use o binario atual do Docker Desktop:

```powershell
$Docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
& $Docker version
& $Docker compose version
Copy-Item .env.example .env
```

Preencha `MAPINATOR_DB_PASSWORD` em `.env` com uma senha local aleatoria de
32+ caracteres **hexadecimais**, sem reutilizar credenciais pessoais.
Mantenha `.env` fora do Git. Nao repetir `Copy-Item` se ja tiver configuracao.

```powershell
& $Docker compose config --quiet
& $Docker compose up --build -d
Invoke-RestMethod http://127.0.0.1:58180/health/ready
```

Baixe explicitamente um extrato real pequeno (nao e seed ficticio):

```powershell
& .\.venv\Scripts\mapinator.exe download --output .\data\sao-paulo.osm
```

O snapshot desta entrega ja foi baixado localmente; o comando recusa sobrescrever
arquivos existentes. Na clonagem, o XML nao vem do Git: baixe-o novamente.
OSM muda com o tempo; o checksum de novos downloads pode ser diferente.

Importe usando o banco do Compose:

```powershell
& $Docker compose exec api mapinator import-osm /app/data/sao-paulo.osm --source-url "https://api.openstreetmap.org/api/0.6/map?bbox=-46.658,-23.563,-46.652,-23.557"
```

**Se usou a Fase 1, reimporte o mesmo XML depois de atualizar a API.** A migracao
2 preserva os dados existentes, mas exige reimportacao para recuperar relations
de restricao de conversao. Checksum repetido atualiza essas relations sem duplicar
o dataset. Sem isso, `/api/v1/routes` retorna 503 `graph_not_ready`.

`/app/data/...` e um caminho **dentro do container Linux**, nao um caminho do host
Windows. Abra http://127.0.0.1:58180 e busque um nome presente no extrato.
Os endpoints estao documentados em http://127.0.0.1:58180/docs.
O formulario de rotas aceita coordenadas ou um nome inequivoco, transporte,
estrategia e preferencias; desenha a rota e mostra instrucoes/alternativas.

## Ativar o pipeline de transito

O broker e opcional; sem workers/dados suficientes, as rotas usam baseline.
Com senha local configurada e Docker Linux funcionando:

```powershell
& $Docker compose --profile traffic config --quiet
& $Docker compose --profile traffic up --build -d
Invoke-RestMethod http://127.0.0.1:58180/health/traffic
```

O perfil cria broker, topicos e workers relay/analytics. A API migra o banco
para versao 5 automaticamente. `/health/traffic` retorna 503 quando degradado;
isso nao torna `/health/ready` indisponivel se o banco principal estiver saudavel.

No browser, autorize GPS e marque separadamente a contribuicao **de carro**.
GPS sem esse opt-in nao entra no pipeline. Sao necessarios ao menos cinco
contribuidores distintos por aresta/janela depois dos filtros; nao ha dados de
transito inventados. O botao de transito consulta os segmentos da rota;
nao ha atualizacao automatica da camada nem da ETA salva nesta fase.

Retencao nominal: posicao atual 5min, GPS bruto no banco 1h e no broker 15min;
historico agregado 90 dias. Expiracao fisica depende da limpeza/servicos,
e backups exigem sua propria politica. Pseudonimizacao e agregacao nao garantem
anonimato. Revogar remove contribuicoes pendentes e amostras no banco;
mensagens ja publicadas nao podem ser recolhidas. Detalhes e limites de exclusao
no [relatorio da Fase 3](docs/phase-03.md).

Sem estilo externo, o mapa exibe ruas importadas sobre um fundo simples; nao
finge oferecer tiles mundiais. A biblioteca MapLibre e carregada de um CDN com
versao fixa. Estilo/tiles opcionais requerem internet e politica de uso adequada.
GPS exige permissao do navegador e HTTPS ou localhost.

## Navegacao, EV e modelo local

```powershell
& $Docker compose --profile traffic --profile navigation up --build -d
Invoke-RestMethod http://127.0.0.1:58180/health/navigation
```

Ative GPS, calcule a rota recomendada e clique em iniciar navegacao.
O botao de desativar revoga GPS/transito e encerra o stream.
Redis usa porta loopback 56379 e senha local do projeto; sem volume persistente.

EV usa coordenadas de origem/destino e perfil **informado pelo usuario**.
Sem perfil, retorna apenas rota comum; com perfil, mostra intervalos/recargas.
Nao converte automaticamente todas as pernas EV em uma sessao de navegacao.

Busca de POIs funciona sem LLM. Para conversa, instale/configure um Ollama
local e escolha explicitamente um modelo licenciado, depois defina seu nome:

```powershell
# Apenas depois de o modelo existir no Ollama local:
$env:MAPINATOR_LLM_MODEL = "<nome-do-modelo-instalado>"
$env:MAPINATOR_LLM_BASE_URL = "http://127.0.0.1:11434"
```

No Compose, configure o nome em `.env`; endpoint padrao
`http://host.docker.internal:11434`, somente host local Docker Desktop.
Nenhum modelo e baixado automaticamente. Ausente/invalido/fora do ar retorna
erro explicito, sem respostas simuladas. O modelo interpreta filtros; fatos
e marcadores vem do OSM. Contexto privado armazena apenas criterios/revisao
por 30min, nao mensagens/GPS; centro e informado novamente em cada chamada.

## Benchmarks

```powershell
& .\.venv\Scripts\mapinator.exe benchmark-osm .\data\sao-paulo.osm --requests 20 --concurrency 2
# Somente em instancia dedicada, pois cria sessoes/rotas com TTL:
& .\.venv\Scripts\mapinator.exe benchmark-http --source=-23.5596178,-46.6562147 --destination=-23.5538367,-46.6481601 --requests 20 --concurrency 2
```

Resultados reais regionais e limites em [phase-07.md](docs/phase-07.md).
Medicao CPU offline nao prova capacidade HTTP/DB/WS/ingestao ou escala global.

## API fora do container

Com o banco do Compose funcionando, exporte a URL usando a mesma senha local
hexadecimal de `.env`; substitua o placeholder abaixo antes de executar:

```powershell
$env:MAPINATOR_DATABASE_URL = "postgresql+psycopg://mapinator:<senha-local>@127.0.0.1:58432/mapinator"
& .\.venv\Scripts\mapinator.exe serve
```

Pare a API do Compose primeiro se for usar a mesma porta. Nao altere o ambiente
global do Windows: variaveis acima valem somente para este terminal.

## Integracao real

Use **outro banco**, exclusivo de testes: o teste importa um dataset sintetico
e altera seu snapshot ativo. Nao aponte a URL de testes para dados reais.

```powershell
# Criar uma vez, quando o Compose estiver funcionando:
& $Docker compose exec db createdb -U mapinator mapinator_test
$env:MAPINATOR_TEST_DATABASE_URL = "postgresql+psycopg://mapinator:<senha-local>@127.0.0.1:58432/mapinator_test"
& .\.venv\Scripts\python.exe -m pytest -q -m integration
```

O teste verifica migracao repetida, PostGIS, ownership, deduplicacao,
localizacao, TTL, importacao atomica, ativacao unica e busca geoespacial.
Tambem verifica carregamento do grafo e persistencia/ownership/expiracao de rotas.
Nenhum resultado desse teste foi declarado aprovado neste host.

A Fase 3 adiciona integracoes reais de outbox/agregacao/revogacao, publicacao
Kafka e execucao conjunta dos workers. O teste Kafka precisa de
`MAPINATOR_TEST_KAFKA_BOOTSTRAP_SERVERS`, apontando para **outro broker descartavel**;
o teste ponta a ponta precisa dos dois endpoints. Os testes alteram dados,
topicos e retencao: nao use infraestrutura compartilhada ou de producao.

Navegacao acrescenta `MAPINATOR_TEST_REDIS_URL`, Redis **descartavel** separado;
o teste de duas instancias tambem exige o banco dedicado. Descoberta acrescenta
teste SQL e `MAPINATOR_TEST_LLM_MODEL` (com endpoint local instalado/configurado).
Resultado final local: **168 passed, 8 skipped**, Ruff/mypy sem erros;
browser com fixtures validou fluxo, nao GPS/modelo fisicos.

## Recursos e parada

- Porta API: `127.0.0.1:58180`; PostgreSQL: `127.0.0.1:58432`.
- Kafka opcional: `127.0.0.1:59092`, sem autenticacao no laboratorio local.
- Redis opcional: `127.0.0.1:56379`, autenticado pela senha local do projeto.
- Volumes Compose `mapinator_mapinator-postgres` e `mapinator_mapinator-redpanda`.
- XML e artefatos em `data`, ignorados; manifesto de origem versionado.
- Sessao do browser dura 24h; somente ultima posicao, expirada em 5min.

```powershell
& $Docker compose --profile traffic --profile navigation stop
```

Nao usar `down -v`: apagaria o banco e o log do broker. Nao expor o laboratorio na Internet:
HA, OIDC, TLS/criptografia de volumes, rate limits distribuidos e observabilidade
distribuida ainda dependem dos marcos posteriores.
