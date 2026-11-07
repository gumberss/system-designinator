# 7. Experimentos observaveis

## Fluxo visual

Suba a stack conforme README e execute `npm run simulate`.
Abra o painel: motoristas aparecem antes de pedir. Observe idade do snapshot,
solicite uma viagem e acompanhe eventos de estado separados de GPS.
Ao chegar, inicie; ao concluir, consulte trajeto salvo depois de um lote.

O motorista volta a oferta na posicao atual, nao no local do seed.
Isso muda o ranking da proxima viagem. O passageiro tem canal GPS proprio.
O painel e simplificado, nao navegacao por ruas.

## Testes reais

```powershell
npm test
npm run test:integration
```

Unitarios conferem quadtree contra busca exaustiva, fronteiras, polos,
antimeridiano, pontos coincidentes e contratos de dominio.
Integracao cria schema e prefixo Redis aleatorios, aplica o schema inicial
limpo, levanta servicos HTTP e ao terminar limpa somente seus recursos.
Nao aponta para producao. Nao execute batches de testes em paralelo com builds
Docker se quiser comparar tempos de forma menos ruidosa.

Bursts usam ate 5 tentativas para 503, preservam a chave e imprimem
integration_overload_responses. O resultado conta pedidos logicos,
nao esconde retries como capacidade sem erros.

## Experimentos de falha manuais

Cada comando afeta somente servicos deste projeto:

```powershell
docker compose stop location-writer
# Execute algumas viagens; backlog cresce em Redis.
docker compose start location-writer
# Pending e novos pontos devem persistir; rota nao deve duplicar.

docker compose stop quadtree-map
# Depois da indisponibilidade, busca/pedido devem indicar falha explicita.
docker compose start quadtree-map
# Mesmo pedido/chave pode retomar; dispatcher recupera searching.

docker compose stop redis
# GPS/busca nao usam checkpoint SQL como se fosse posicao atual.
docker compose start redis

docker compose stop postgres
# Sem confirmacao falsa de viagem/estado; writer nao confirma lote SQL falho.
docker compose start postgres
```

Nao sao testes de failover ou RPO zero. Redis AOF e PostgreSQL possuem garantias
diferentes; nao desligue a maquina esperando que ACK de cache seja commit SQL.
Depois de recriar container, Nginx reavalia DNS Docker em ate ~5s.
Algumas falhas no gateway podem aparecer como 502/504, nao somente 503.

## GPS vencido

Pare apenas o simulador e espere mais de 15s. Motoristas nao devem continuar
na oferta. Corrida ja atribuida nao e automaticamente desfeita.
Reiniciar simulador recupera a ultima sequencia; se Redis foi totalmente
perdido, baseline persistido nao recupera amostras nunca gravadas.

## Consultas de diagnostico SQL

```sql
SELECT driver_id,count(*) FROM trips
WHERE status IN ('assigned','arrived','in_progress')
GROUP BY driver_id HAVING count(*)>1;

SELECT passenger_id,count(*) FROM trips
WHERE status IN ('searching','assigned','arrived','in_progress')
GROUP BY passenger_id HAVING count(*)>1;

SELECT d.id,d.busy FROM drivers d
WHERE d.busy <> EXISTS (SELECT 1 FROM trips t WHERE t.driver_id=d.id
  AND t.status IN ('assigned','arrived','in_progress'));

SELECT count(*) FROM trip_events WHERE published_at IS NULL;
SELECT status,count(*) FROM trips GROUP BY status;
SELECT trip_id,actor,count(*) FROM route_points GROUP BY trip_id,actor;
```

## Diagnosticos Redis e HTTP

```powershell
docker compose exec redis redis-cli XLEN rides:gps-stream
docker compose exec redis redis-cli XPENDING rides:gps-stream postgres-writer
docker compose exec redis redis-cli HLEN rides:latest:driver
Invoke-RestMethod http://127.0.0.1:58100/api/system/map
docker compose logs --tail 50 location-writer trip-manager location-manager
```

XDEL remove payload confirmado, mas nao reseta o ultimo ID do stream.
Hash retido nao significa GPS fresco; confira receivedAt/fresh.
Checkpoint atrasado nao significa que o stream ao vivo parou.
Outbox publicada nao significa que todos os browsers receberam o aviso.

## Metricas para evoluir

Ainda nao ha Prometheus/exporter ou tracing distribuido.
Medir: idade do snapshot, tamanho/idade do delta, candidatos, freshness,
GPS aceitos/descartados, backlog e pending, atraso do checkpoint, outbox,
conexoes realtime, reconnects, bytes/client, latencia por servico,
pool wait, conflitos, taxa de atribuicao, expiracoes e cancelamentos.

Registrar SLO por caminho: mapa, pedido, claim, GPS e historico.
Um backlog normal de 15s nao deve disparar o mesmo alerta de writer parado
ha minutos. Logs de producao precisam correlacao e sanitizacao de PII.
