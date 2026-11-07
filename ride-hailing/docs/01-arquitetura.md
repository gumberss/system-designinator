# 1. Arquitetura e decisoes da versao de servicos

## Requisitos

Busca de motoristas antes do pedido; GPS de passageiros e motoristas ~4s;
indice quadtree e checkpoint ~15s; selecao de motorista; acompanhamento,
chegada, inicio, conclusao e cancelamento. Idempotencia e exclusividade devem
valer mesmo com chamadas simultaneas e replicas.

A carga hipotetica e os recursos estao no [dimensionamento](10-estimativa-de-recursos.md).
Os limites do codigo local nao pretendem atender 10k pedidos/s ou 100M leituras/s.

## Fronteiras implementadas

| Processo | Dono de qual responsabilidade | O que nao decide |
|---|---|---|
| Nginx | roteamento e balanceamento least_conn de request vehicle x2 | exclusividade, retries de negocio |
| Request vehicle | criar intencao e pedir matching | nao escolhe via memoria local |
| Find driver | gerar/rankear candidatos por distancia | nao confirma motorista sozinho |
| Trip manager | corrida, claim, estados, idempotencia e outbox | nao recebe cada heartbeat GPS |
| Location manager | hash, sequencia, proximidade, SSE de posicao | nao reserva motorista |
| Quadtree map | snapshot espacial e consulta por raio | nao e fonte da ultima posicao |
| Location writer | checkpoint e trajetos, consumo recuperavel | nao altera estado da corrida |

Cada papel executa em processo/container separado, a partir de uma imagem.
Ha um unico PostgreSQL e um unico Redis locais. Tabelas compartilhadas reduzem
custo operacional do lab: sao fronteiras de responsabilidades, nao bancos
fisicamente independentes por microservico. O writer le janelas das corridas
para correlacionar trajeto. Extrair esse acoplamento exige um protocolo de
eventos/ownership, nao simplesmente trocar a connection string.

## ADRs

### ADR-01: quadtree para candidatos, PostgreSQL para exclusividade

O indice organiza pontos por quadrantes e busca nas folhas intersectadas.
A posicao atual vem do hash; estado online/busy/categoria vem do trip manager.
O claim relera a ultima posicao e revalida o motorista sob lock SQL.
UNIQUE parcial permanece como defesa final, inclusive para SQL direto.
Alternativa: continuar PostGIS para todas as buscas. E mais simples, mas nao
demonstra o caminho em memoria e aumenta trabalho do banco sob GPS intenso.

**No codigo:** [consulta da quadtree](../src/services/quadtree-map/app.ts#L21),
[candidatos e delta](../src/services/location-manager/app.ts#L14),
[claim transacional](../src/shared/store.ts#L62) e
[indices unicos de corridas ativas](../infra/001-schema.sql#L39).

### ADR-02: GPS fora da transacao de corrida

Ingestao valida sequencia e observedAt, e grava hash + stream + delta +
publicacao atomicos num script Redis. Nao abre transacao SQL por GPS.
Checkpoint duravel ocorre em lotes. Custo: ACK de Redis nao significa commit
PostgreSQL; existe uma janela de durabilidade documentada.

**No codigo:** [escrita atomica no Redis](../src/shared/redis.ts#L46),
[endpoint de ingestao](../src/services/location-manager/app.ts#L56) e
[flush que commita antes de ACK/XDEL](../src/services/location-manager/writer.ts#L42).

### ADR-03: snapshot espacial ~15s e delta recente

A arvore e reconstruida em memoria e trocada inteira, sem expor estrutura
parcial. O delta inclui motoristas atualizados desde o inicio do snapshot.
Somente hash + snapshot sem delta poderia omitir um motorista que entrou no
raio ou apareceu entre rebuilds. Veja [quadtree](09-quadtree-e-delta.md).
Alternativa: quadtree mutavel por update. Aumentaria manutencao por heartbeat,
split/merge e sincronizacao entre consultas.

**No codigo:** [rebuild e troca do snapshot](../src/services/quadtree-map/app.ts#L9),
[busca espacial com poda e distancia](../src/services/quadtree-map/quadtree.ts#L40) e
[uniao com o delta](../src/services/location-manager/app.ts#L14).

### ADR-04: hash coalescido, trajetos nao coalescidos

O ultimo GPS e substituivel; o trajeto precisa dos intermediarios.
Por isso o hash guarda um valor por entidade, mas o stream guarda cada amostra
aceita ate o writer confirmar. Salvar somente o hash a cada 15s perderia
aproximadamente 3 de cada 4 pontos se GPS chega a cada 4s.

**No codigo:** [hash e stream recebem a amostra](../src/shared/redis.ts#L63),
[checkpoint coalescido](../src/services/location-manager/writer.ts#L54) e
[persistencia de todas as amostras de rota](../src/services/location-manager/writer.ts#L76).

### ADR-05: cursor por corrida, nao contador global

Cada corrida tem event_sequence, incrementado sob o mesmo lock da entidade.
Eventos de corridas diferentes nao disputam uma linha global.
Ordem global nao e requisito do acompanhamento de uma viagem.
Nao usar BIGSERIAL global como se alocacao fosse ordem de commit.

**No codigo:** [incremento do cursor na linha da corrida e insercao do evento](../src/shared/store.ts#L53),
[consulta ordenada pelo cursor](../src/shared/store.ts#L177) e
[replay do cursor no endpoint SSE](../src/services/trip-manager/app.ts#L72).

### ADR-06: log duravel de estado + pub/sub como aviso

trip_events e outbox no mesmo commit de negocio. Um publisher envia um aviso
Redis e marca publicado. SSE recupera eventos SQL por cursor ao receber aviso,
na conexao inicial ou em reconciliacao a cada 10s.
Pub/sub nao e duravel: perder o aviso nao pode perder o estado.
GPS usa ultimo valor, sem prometer replay de todos os pontos pela conexao SSE.

**No codigo:** [publicacao da outbox](../src/shared/store.ts#L185),
[SSE consultando o log por cursor](../src/services/trip-manager/app.ts#L99) e
[assinaturas Redis compartilhadas por processo](../src/shared/redis.ts#L92).

### ADR-07: matching apos persistir, sem transacao SQL atravessando HTTP

O request vehicle cria searching antes de chamar find driver.
Falha de rede pode deixar um pedido persistido e devolver 503; retry com a
mesma chave e o dispatcher convergem para a mesma intencao.
Nenhum lock SQL fica aberto durante HTTP para outro servico.
Nao ha transacao distribuida escondida nem compensacao que apague uma viagem.

**No codigo:** [pedido persistido antes de chamar find-driver](../src/services/request-vehicle/app.ts#L12),
[transacao idempotente da intencao](../src/shared/store.ts#L90) e
[dispatcher recuperavel em lotes](../src/shared/store.ts#L128).

### ADR-08: fila local com backpressure, nao descarte silencioso

Redis AOF everysec e noeviction; stream nao e truncado antes do commit SQL.
Ha limites locais de entidades, backlog e candidatos. Exceder devolve 503.
Isso torna limites visiveis, em vez de prometer escala ilimitada.
Kafka/log particionado e uma evolucao de producao, nao dependencia adicional
necessaria para entender este laboratorio.

**No codigo:** [limites explicitos no script de ingestao](../src/shared/redis.ts#L55),
[limite de delta sem truncamento silencioso](../src/services/location-manager/app.ts#L18) e
[AOF/noeviction da instancia local](../compose.yaml#L47).

### ADR-09: load balancer nao e garantia transacional

Nginx balanceia duas request APIs. Nao refaz POST automaticamente.
DNS Docker e reavaliado para sobreviver a troca de IP de containers.
Outros servicos ainda sao instancia unica. HA, autoscaling e failover nao
foram implementados apenas por existir um gateway.

**No codigo:** [DNS dinamico do gateway](../infra/nginx.conf#L1),
[balanceamento least-connections](../infra/nginx.conf#L4),
[POST sem retry transparente](../infra/nginx.conf#L25) e
[segunda replica de request-vehicle](../compose.yaml#L96).

### ADR-10: manter modelo de negocio original

Mesma categoria, raio 5km, menor distancia Haversine, empate por ID, sem aceite
humano. O PostGIS continua no storage geografico e em exercicios SQL, nao na
busca quente do painel. Distancia nao e ETA rodoviario. Armazenar um ETA falso
com nome convincente ensinaria o contrato errado.

**No codigo:** [filtro por raio e ordenacao dos candidatos](../src/services/location-manager/app.ts#L14),
[distancia geodesica](../src/shared/domain.ts#L79) e
[preco deterministico do pedido](../src/shared/domain.ts#L86).

## Fora do escopo atual

Autenticacao, autorizacao, navegacao por ruas, pagamento, fraude, ofertas com
lease, migracao de autoridade entre cidades, Redis Cluster e operacao global.
Requisitos de producao incluem limites por identidade, LGPD, TLS, secrets,
RPO/RTO e custos aferidos. Nenhum recurso de cloud foi provisionado.
