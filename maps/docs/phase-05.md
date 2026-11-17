# Fase 5: planejamento EV regional

Implementado em `mapinator.ev` e `mapinator.ev_api`, sem migracao ou alteracao do
contrato de rotas comuns. Todos os testes desta fase usam **fixtures sinteticas
rotuladas**, nao um catalogo de estacoes ou telemetria reais.

## Integracao e contrato HTTP

O coordenador da API registra:

```python
from mapinator.ev_api import create_ev_router

app.include_router(create_ev_router(repo, mutation_guard, execute))
```

Assinatura exata:

```python
create_ev_router(
    repo: Repository,
    mutation_guard: Callable[..., Identity],
    execute: Callable[
        [Callable[[], dict[str, object]]],
        Awaitable[dict[str, object]],
    ],
) -> APIRouter
```

`mutation_guard` e uma dependencia FastAPI: autenticacao, origin e CSRF ficam
sob responsabilidade da API existente. `execute` admite trabalho no pool de
processos compartilhado e limita concorrencia/timeout externo; recebe um
`functools.partial` pickleable de `plan_ev_trip`, nunca uma closure com banco.
Leitura do repositorio e persistencia usam `asyncio.to_thread`.

`POST /api/v1/ev/trips` retorna **201**. Coordenadas sao objetos
`source: {latitude, longitude}` e `destination: {latitude, longitude}`;
internamente, tuplas seguem GeoJSON `(longitude, latitude)`. Coordenadas
de pernas e carregadores tambem usam `latitude`/`longitude`; `lat`/`lon`
nao sao aliases aceitos. `vehicle` e opcional. Campos:

| Campo | Unidade/restricao |
|---|---|
| `battery_capacity_kwh` | kWh, positivo, ate 1000 |
| `initial_soc_percent` | 0 a 100 |
| `reserve_percent` | 0 inclusive a 100 exclusive; nao supera SOC inicial |
| `consumption_kwh_per_100km` | kWh/100km, positivo, ate 1000 |
| `max_charging_power_kw` | kW, positivo, ate 2000 |
| `connectors` | lista nao vazia de nomes OSM explicitamente aceitos |
| `calibrated_charging_curve` | opcional, lista `{soc_percent, power_kw}` |

Conectores aceitos: `type1`, `type1_combo`, `type2`, `type2_cable`,
`type2_combo`, `chademo`, `tesla_supercharger`, `tesla_destination`.
Compatibilidade e explicita: nao inferimos adaptadores. Curva calibrada cobre
0–100% com SOC estritamente crescente e potencia positiva limitada pelo
maximo declarado. Campos extras, strings numericas, NaN, infinitos e valores
invalidos sao rejeitados. Perfil parcial nao ganha parametros padrao.

Request minimo valido (solicita somente fallback comum). As coordenadas abaixo
sao da fixture sintetica; o cliente deve fornecer coordenadas da regiao
realmente importada:

```json
{
  "source": {"latitude": 0.0, "longitude": 0.0},
  "destination": {"latitude": 0.0, "longitude": 0.3}
}
```

Request minimo para avaliar EV, sem curva opcional. Os parametros abaixo sao
da fixture, **nao devem virar valores padrao de um veiculo real**:

```json
{
  "source": {"latitude": 0.0, "longitude": 0.0},
  "destination": {"latitude": 0.0, "longitude": 0.3},
  "vehicle": {
    "battery_capacity_kwh": 5.0,
    "initial_soc_percent": 90.0,
    "reserve_percent": 10.0,
    "consumption_kwh_per_100km": 20.0,
    "max_charging_power_kw": 50.0,
    "connectors": ["type2_combo"]
  }
}
```

Curva opcional dentro de `vehicle`, somente quando calibrada pelo usuario:

```json
{
  "calibrated_charging_curve": [
    {"soc_percent": 0.0, "power_kw": 50.0},
    {"soc_percent": 80.0, "power_kw": 50.0},
    {"soc_percent": 100.0, "power_kw": 10.0}
  ]
}
```

Schema: lista de 2–32 objetos sem campos extras; `soc_percent` numerico
finito em 0–100, `power_kw` numerico finito positivo; SOC inicial 0, final
100 e estritamente crescente; nenhuma potencia excede
`max_charging_power_kw`. Nao enviar curva inventada quando ausente.

### Respostas

Perfil ausente/incompleto:

- `ev_planning_available=false`, `feasible=null`;
- `fallback_reason="missing_vehicle_parameters"` e lista
  `missing_vehicle_parameters`;
- `ordinary_route` preserva o resultado normal de `calculate`, com `route_id`
  e `dataset_id` apos persistencia no owner autenticado;
- `legs=[]`, `charging_stops=[]`; nenhuma viabilidade EV e alegada.

Sucesso EV:

- `ev_planning_available=true`, `feasible=true`, `ordinary_route=null`;
- `dataset_id`, `provenance` com OSM, checksum do grafo, cobertura e licenca;
- `legs`: cada perna possui `route` normal, coordenadas, intervalo de energia,
  SOC de partida/chegada e respectivos intervalos, reserva e SOC minimo;
- `charging_stops`: IDs `node/...`/`way/...`, tags de origem, conector, potencia
  nominal/limite efetivo, carga adicionada e intervalo, SOC, tempo/intervalo,
  `before_leg`, `availability="unknown"` e timestamp de disponibilidade null;
- `energy`: `lower_kwh`, `estimate_kwh`, `upper_kwh`; `energy_model` explicito;
- `driving_s`, `detour_s`, `access_s`, `charging_s`, `total_time_s`,
  `charging_time_interval_s` e `time_components`; desvio **ja esta incluido**
  em driving e nao deve ser somado novamente. `queue_s=null`;
- `search` com limites/contadores e truncamento; `excluded_chargers`, `warnings`.

O wrapper EV e suas pernas **nao sao salvos como uma rota continua comum**.
Somente o fallback comum chama `save_route`. Navegacao do itinerario EV inteiro,
perfil privado persistente e historico de viagens nao fazem parte desta fase.

Falhas mantem `DomainError`: `route_not_found`/`outside_coverage` do roteador,
`ev_trip_unreachable` (422, nenhuma solucao nos candidatos limitados),
`ev_budget_exceeded` (503, sem confundir esgotamento com inviabilidade),
e `ev_graph_mismatch` (503). A API hospedeira mantem seu handler de problemas.
Falha SQL exclusivamente no snapshot de trafego produz baseline com aviso
explicito; falha no dataset ou persistencia nao e ocultada.

## Dados, energia e busca

Catalogo deriva **tags reais** `amenity=charging_station` de nodes/ways no
dataset importado; nao depende de `places`, que pode estar vazio no repositorio.
Somente potencias positivas com unidades W/kW/MW em
`socket:<conector>:output` sao usadas. Numeros sem unidade, saida desconhecida,
socket ausente explicitamente e incompatibilidade nao viram potencia inventada.
Potencia generica da estacao nao e atribuida arbitrariamente a um socket.
Sites privados, acesso condicional e estacoes marcadas inativas sao excluidos.

Um node precisa participar de via permitida a carros. Ways de estacao usam um
membro real conectado ao grafo de carros, identificado como
`mapped_site_road_member`. Sem evidencia de acesso, sao excluidos; nao se
inventa uma entrada dirigivel a partir de um centroide proximo da estrada.
Essa politica conservadora pode excluir carregadores validos mal mapeados.
OSM nao fornece ocupacao ao vivo: disponibilidade nunca significa vaga livre.
Horario e preservado mas nao interpretado como garantia de operacao.

Consumo nominal soma distancia por aresta e consumo informado, com penalidade
parametrica explicita para velocidade efetiva acima de 90km/h; velocidades
usam o mesmo snapshot de trafego do roteador. Distancia do snap entra como
margem de acesso. Intervalo 0.85–1.30 amplia a incerteza quando elevacao/clima
nao existem. Esses fatores sao **heuristicas nao calibradas**, nao limites
fisicos certificados. Nao se afirma temperatura, elevacao, bateria ou
regeneracao inexistente. Reserva e verificada em todas as pernas usando
energia superior; consumo monotonico sem regeneracao mantem o minimo na
chegada. Intervalos de SOC propagam a incerteza acumulada.

Dijkstra em estados (local, SOC, aresta anterior, numero de cargas), com
dominancia custo/SOC, compara caminhos fastest/shortest dirigidos e diferentes
alvos de carga; nao escolhe simplesmente a estacao mais proxima. O custo e
tempo de direcao do itinerario + margem de acesso + tempo superior de carga.
Restricoes de conversao tambem sao verificadas entre pernas, evitando reset
indevido ao parar num carregador.

Tempo integra `capacidade / min(potencia_socket, maximo_veiculo, curva_SOC)`
com interpolacao linear da curva calibrada. Sem curva, taper explicitamente
estimado: potencia maxima ate 80%, caindo linearmente a 20% em 100%; limite
superior de tempo recebe fator 1.30 e inferior ideal e exposto. Incerteza do
SOC amplia tambem os intervalos de carga/tempo. Fila e plug-in nao conhecidos
nao sao inventados ou incorporados silenciosamente.

## Limites e verificacao

Padroes: prazo total **6 segundos**, 12 candidatos, 384 chamadas de roteamento,
20.000 estados expandidos/enfileirados, 6 cargas e alvos SOC de 5% mais energia
exata necessaria para a proxima perna. Prazo e compartilhado: tracer Python
temporario, restaurado em `finally`, verifica tambem construcao/snapping e
`calculate` em andamento, nao somente entre chamadas. O job deve rodar em
processo isolado; chamada C nao interrompivel ainda requer timeout externo do
executor. Limites customizados nunca permitem prazo de 30 segundos ou mais.

Catalogo ordenado pelo limite geometrico de desvio; truncamento e declarado.
Nao ha otimo global nem prova de inviabilidade fora dos candidatos/rotas
comparados. O rebuild regional por par e limitado, mas nao e preprocessamento
para escala mundial. O modelo nao certifica autonomia no mundo real.

Validacao dirigida:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_ev.py tests\test_ev_api.py
.\.venv\Scripts\python.exe -m ruff check --no-cache src\mapinator\ev.py src\mapinator\ev_api.py tests\test_ev.py tests\test_ev_api.py
.\.venv\Scripts\python.exe -m mypy src\mapinator\ev.py src\mapinator\ev_api.py tests\test_ev.py tests\test_ev_api.py
```

Cobertura: reserva em cada perna, duas cargas realmente necessarias, conectores,
potencia desconhecida, estacao way/acesso, taper integrado, escolha nao greedy,
fallback, one-way/conversao, deadline em andamento, limites de filas/chamadas,
pickle e pool real no endpoint, autenticacao/CSRF por callbacks e persistencia
exclusivamente da rota comum. Sem validacao com bateria/ocupacao reais.
