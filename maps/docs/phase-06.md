# Fase 6: descoberta deterministica e interpretacao local

## Entrega e evidencia

Foram adicionados `discovery.py`, `discovery_api.py`, `discovery_store.py`,
`migrations\005_discovery.sql` e quatro arquivos `tests\test_discovery*.py`.
Os modulos foram integrados na API principal, frontend e limpeza periodica.
Settings atuais expoem `MAPINATOR_LLM_MODEL` e `MAPINATOR_LLM_BASE_URL`;
os outros limites usam os defaults documentados abaixo.

Validacao focada: **60 testes passaram, 2 integracoes foram puladas**; Ruff
sem cache e mypy estrito passaram nos sete arquivos Python novos. Pylance
nao encontrou erros de sintaxe nos tres modulos de producao.
O aviso de depreciacao Starlette/httpx do TestClient permanece.
Com os dois testes de arquitetura existentes, a ultima execucao terminou
com **62 passed, 2 skipped**.

As integracoes reais **continuam pendentes**: `MAPINATOR_TEST_LLM_MODEL` e
`MAPINATOR_TEST_DATABASE_URL` nao estavam configurados. Uma consulta somente
leitura, com timeout de dois segundos, a `127.0.0.1:11434/api/tags` terminou
em `ConnectTimeout`. Nenhum servico foi iniciado/parado e nenhum modelo foi
baixado. Doubles HTTP, repositorio e conversas dos testes unitarios nao
constituem evidencia de execucao de modelo ou PostgreSQL reais.

## Contratos para integracao

A aplicacao hospedeira registra o router e fornece sua propria dependencia
de autenticacao, Origin e CSRF:

```python
create_discovery_router(
    repo: Repository,
    mutation_guard: Callable[..., Identity],
    llm: LocalInterpreter | None,
    store: ConversationStore | None,
) -> APIRouter
```

Os endpoints sao funcoes sincronas, executadas no thread pool do FastAPI.
O router nao importa `api.py`, nao registra handlers globais e propaga
`DomainError` e erros SQL para os handlers da aplicacao hospedeira.
Busca estruturada nao usa a dependencia de mutacao; conversa sempre a usa.

Configuracao exata do adaptador, sem Settings ou leitura implicita de env:

```python
LocalInterpreter(
    model: str,
    base_url: str = "http://127.0.0.1:11434",
    *,
    timeout_s: float = 15,
    max_response_bytes: int = 32768,
    max_concurrency: int = 2,
)
LocalInterpreter.interpret(message: str, criteria: Criteria) -> CriteriaPatch
PostgresConversationStore(engine: Engine, *, ttl_s: int = 1800)
```

Mapeamento sugerido para os Settings que a aplicacao hospedeira controla:
`MAPINATOR_LLM_MODEL`, `MAPINATOR_LLM_BASE_URL`, `MAPINATOR_LLM_TIMEOUT_S`,
`MAPINATOR_LLM_MAX_RESPONSE_BYTES`, `MAPINATOR_LLM_MAX_CONCURRENCY` e
`MAPINATOR_CONVERSATION_TTL_S`, correspondendo aos parametros acima.
Sem modelo configurado, a aplicacao deve injetar `llm=None`, nunca um double.
Conversa retorna 503 `llm_not_configured`; com modelo mas sem store, retorna
503 `conversation_store_unavailable`.

### POST /api/v1/places/search

Request `PlaceSearchRequest`:

```json
{
  "center": {"latitude": -23.55, "longitude": -46.63},
  "criteria": {
    "query": null,
    "categories": ["cafe"],
    "radius_m": 2000,
    "open_now": false,
    "min_rating": null,
    "work_friendly": false,
    "quiet": false,
    "wifi": false
  },
  "open_at": null,
  "limit": 20,
  "cursor": 0
}
```

`center` e obrigatorio. Os outros campos usam os defaults mostrados.
Raio: 1–50.000 m; pagina: 1–50 itens; cursor: 0–100.000.
`open_at`, quando informado, exige timezone e filtra somente locais
verificadamente abertos naquele instante. Nao pode coexistir com
`criteria.open_now=true`. Sem esses filtros, horarios desconhecidos nao
excluem locais. Query e substring case-insensitive de nome/endereco,
com 1–200 caracteres quando nao nula.

Categorias aceitas nos criterios:
`cafe`, `restaurant`, `fast_food`, `bar`, `pub`, `library`,
`coworking_space`, `park`, `hotel`, `hostel`, `museum`, `supermarket`,
`bakery`, `convenience`, `pharmacy`, `charging_station`, `fuel`, `cinema`.
A lista aceita ate oito categorias distintas; lista vazia nao filtra categoria.

Response 200 `PlaceSearchResponse`:
`criteria`, `items`, `next_cursor`, `evaluated_at`, `dataset_id`,
`coverage="active_local_osm_dataset"`, `warnings`, `explanation`, `license`.
Cada item possui:
`source_id`, `dataset_id`, `name`, `latitude`, `longitude`, `location_kind`,
`category`, `address`, `distance_m`, `opening_hours`, `timezone`, `open_at`
(bool ou null), `rating`, `work_friendly`, `quiet`, `wifi`, `evidence` e
`source="openstreetmap"`.

### POST /api/v1/places/conversation

Request `ConversationRequest`:
`message` (1–2.000 caracteres, nao branco), `center` obrigatorio,
`conversation_id=null`, `revision=null`, `criteria=null`, `limit=20`.
Na primeira chamada, `criteria` opcional fornece os filtros iniciais.
No follow-up, UUID e revision devem vir juntos; `criteria` nao pode ser
reenviado. O centro deve ser fornecido novamente: nao e persistido.

Response 200 `ConversationResponse` estende `PlaceSearchResponse` com
`conversation_id`, `revision`, `expires_at` e
`interpretation_source="local_ollama"`. A primeira revision e 1.
Somente campos presentes no `CriteriaPatch` substituem os criterios antigos.
`query` e `min_rating` podem ser limpos explicitamente com null; null em
outros campos e invalido. Revision obsoleta retorna 409
`conversation_revision_conflict`, inclusive se outra instancia atualizar
durante a chamada ao modelo. Outra identidade ou TTL vencido recebe 404
`conversation_not_found`; nenhum contexto privado e retornado.

## Retrieval, horarios e fronteira de IA

O retrieval usa `repo.routing_dataset()`, reconstruindo POIs nomeados das
tags de nodes/ways, porque o adaptador PostgreSQL retorna `places=()`.
Nao usa Nominatim, Overpass, ratings externos ou locais inventados.
Distancia usa Haversine em metros e coordenadas WGS84, incluindo antimeridiano.
Filtros antecedem o ranking deterministico por distancia, nome e ID OSM.
Ways usam um vertice OSM real identificado como `osm_way_vertex`; distancia
nao representa a distancia minima a toda a geometria ou um centro comercial.
POIs sem nome e POIs em relations nao sao recuperados nesta implementacao.

Horarios suportados: `24/7`, `off`/`closed`, regras semanais Mo–Su, listas e
ranges de dias, varios intervalos diarios, `24:00` como termino e travessia
de meia-noite. Regras semanais exigem tag OSM `timezone` valida; nao se
deduz timezone de coordenadas ou do servidor. A conversao do instante usa
ZoneInfo, incluindo DST. `24/7` e fechado integral independem de timezone.
Feriados, datas, sunrise/sunset, comentarios, regras sobrepostas e conflitos
de fechamento com intervalos noturnos permanecem desconhecidos. Nao e um
parser completo de `opening_hours`. O status e evidencia do horario OSM,
nao verificacao operacional ao vivo.

Ratings, quietude e adequacao para trabalhar permanecem null, inclusive
quando tags nao padronizadas alegam esses fatos. Seus filtros nao aceitam
unknown. Wi-Fi e true somente com `internet_access=wlan`, false com `no`,
e null nos demais casos; `internet_access=yes` nao prova Wi-Fi.

O modelo recebe **somente mensagem e criterios limitados**, nunca center,
GPS, historico de mensagens ou fatos recuperados. Ele interpreta filtros;
retrieval e explicacao por template ocorrem depois, deterministicamente.
Nao ha segunda chamada generativa, ferramentas, ordenacao pelo modelo ou
execucao de comandos. Campos de lugares, IDs, fatos ou tools na saida sao
rejeitados, nao executados.

O adaptador chama Ollama real em HTTP loopback (`127.0.0.1`, `localhost`
ou `::1`) ou no hostname fixo Docker Desktop `host.docker.internal`,
`/api/chat`, sem proxy de ambiente, redirects ou streaming de
tokens. Aplica schema JSON, 512 tokens de saida, contexto 4096, limite de
bytes, timeout HTTP e verificacao de prazo durante leitura. JSON duplicado,
nao finito, invalido, respostas nao finais, MIME inesperado, compressao ou
tool calls sao rejeitados. Tres falhas abrem o circuito por 30 segundos.
Modelo ausente/indisponivel produz 503, saida invalida produz 502; nao existe
fallback ficticio em producao. A mensagem pode conter texto de localizacao
digitado pelo proprio usuario; o centro estruturado nunca e anexado.
Nomes de modelo com sufixo `-cloud` ou `:cloud` sao recusados. O operador
deve fornecer um daemon/modelo realmente local, nao um proxy para terceiros;
o adaptador nao atesta a configuracao interna do daemon.
O default nativo permanece `http://127.0.0.1:11434`; Compose pode injetar
`http://host.docker.internal:11434` para alcancar Ollama no host local.
Outros hostnames Docker ou DNS externos continuam proibidos.

## Persistencia e limpeza

```python
@dataclass(frozen=True)
class Conversation:
    conversation_id: UUID
    owner_id: UUID
    criteria: Criteria
    revision: int
    expires_at: datetime


class ConversationStore(Protocol):
    def create(self, owner_id: UUID, criteria: Criteria, now: datetime) -> Conversation: ...
    def get(self, owner_id: UUID, conversation_id: UUID, now: datetime) -> Conversation | None: ...
    def update(
        self,
        owner_id: UUID,
        conversation_id: UUID,
        revision: int,
        criteria: Criteria,
        now: datetime,
    ) -> Conversation: ...
    def cleanup(self, now: datetime) -> None: ...
```

Migration 005 cria `discovery_conversations`, FK para `browser_sessions`
com cascade, revision positiva, JSONB de criterios e indices owner/expiry.
O store usa transacoes, lock de linha e comparacao otimista de revision no
UPDATE. TTL e renovado no commit, default 30 minutos, configuravel de
60 segundos a 24 horas. Somente criterios sao armazenados; sem mensagens
ou center. Expiracao e revisao sao verificadas novamente apos inferencia.
O runner de migracoes e a chamada periodica de `store.cleanup(now)` devem
ser integrados pela aplicacao hospedeira. Nao existe store de memoria
no pacote de producao; o double fica exclusivamente nos testes.

## Reproduzir a validacao

Na pasta `maps`, PowerShell:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_discovery.py tests\test_discovery_llm.py tests\test_discovery_api.py tests\test_discovery_integration.py tests\test_architecture.py
```

Para o teste real de modelo, definir `MAPINATOR_TEST_LLM_MODEL` com um
modelo ja instalado e, opcionalmente, `MAPINATOR_TEST_LLM_BASE_URL`
(default `http://127.0.0.1:11434`). Para SQL, definir
`MAPINATOR_TEST_DATABASE_URL` apontando exclusivamente para banco PostGIS
isolado e descartavel; o teste cria/aplica schema e exercita transacoes,
duas atualizacoes concorrentes, ownership, TTL, limpeza e cascade.
Sem essas variaveis, os testes sao explicitamente pulados.

Nao foram medidos qualidade de interpretacao de modelo real, desempenho
de retrieval em extrato grande, garantia mundial de cobertura ou experiencia
de marcadores no browser. O frontend e a integracao da aplicacao nao fazem
parte destes arquivos.
