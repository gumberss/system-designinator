# Fase 4: navegacao experimental

Implementados Redis TTL, sessoes owner-scoped em migration 004, HTTP/WS,
progresso por projecao no caminho restante, ETA por duracao de aresta,
histerese de desvio, recalc e parada. Nao e navegacao certificada.

## Contratos

- `POST /api/v1/navigation/start`: `route_id`, `tracking_session_id`,
  `tracking_consent=true`, `traffic_consent=false` por padrao.
  Exige rota privada ativa e rastreamento ja autorizado do mesmo usuario.
  Contribuicao somente de carro. Rotas antigas sem preferencias precisam ser
  recalculadas antes de iniciar, para nao perder restricoes durante reroute.
- `POST /api/v1/navigation/location`: observacao GPS da fase 1, acrescentando
  `navigation_session_id`. Validacao temporal, deduplicacao e ordem preservadas.
- `POST /api/v1/navigation/stop`: `session_id`; idempotente e revoga GPS/transito.
  Exclusao comum de localizacao tambem para navegacao.
- `WS /api/v1/navigation/{id}/stream?resume_sequence=N`: cookie autenticado
  e Origin permitido, sem token na URL. Snapshot completo na reconexao,
  atualizacoes `eta_updated`, `route_updated`, `arrived`, heartbeat e erro.
  Resume e um hint, nao replay de todos os eventos perdidos.
- `/health/navigation`: verifica Redis; degradacao nao bloqueia rotas comuns.

Estado inclui revisao, progresso, distancia restante, ETA nullable e proxima
instrucao. A rota salva permanece um snapshot; reroute cria outra rota privada,
preservando modo, estrategia e preferencias de evitar pedagio/rodovia.
Resposta atrasada do GPS/stream nao restaura sessao parada.

## Algoritmos, estado e limites

Projecao em segmentos, nao somente vertices, com progresso monotono e janela
de tolerancia de 20m para evitar saltos para trechos ja percorridos. GPS com
precisao pior que 35m nao atualiza progresso/ETA, e informa a limitacao.
Desvio acima de max(30m,2*precisao), duas amostras e cooldown de 30s para recalc.
Retorno ate max(15m,precisao) limpa a histerese. Chegada exige ate 25m restantes
e ate 15m do destino; o browser para/revoga GPS ao recebe-la.

Redis armazena apenas estado mais recente derivado, sem historico de pontos.
CAS atomico por revisao, TTL de 300s renovado por observacao, chave por
proprietario/sessao. SQL e autoridade para consentimento/ownership e rota;
parada remove a sessao duravel antes de apagar cache. Falha de exclusao
e explicita: parada logica nao depende de um Redis ainda disponivel.
Consentimento de transito e amostras SQL seguem as regras da fase 3.

Restart/expiracao do Redis requer iniciar navegacao de novo: nao retorna
progresso zero fingindo recuperar estado perdido. Redis nao tem volume
persistente neste laboratorio. Dados derivados expiram logicamente em 5min;
exclusao fisica continua dependente dos servicos/backups.

Stream consulta estado a cada 0,5s, envia somente revisoes novas e heartbeat
a cada 10s. Uma escrita por vez, timeout de 2s para consumidor lento, sem fila
ilimitada. Cliente reconecta com snapshot; apos cinco reconexoes sem dados
validos, para GPS e informa erro. Nao e pub-sub distribuido nem SLA medido.
CPU compartilha o pool de dois processos com rotas/EV; saturacao retorna 429.
Falha de recalc deixa `off_route`, ETA desconhecida e aviso, nao linha reta.

## Executar

Configuracao de senha/importacao conforme [README](../README.md):

```powershell
$Docker = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
& $Docker compose --profile traffic --profile navigation up --build -d
Invoke-RestMethod http://127.0.0.1:58180/health/navigation
```

API 58180, PostGIS 58432, Redis 56379 e Kafka 59092 em loopback.
Redis exige a senha local do projeto; nao e credencial pessoal.
Host API requer `MAPINATOR_REDIS_URL=redis://:<senha>@127.0.0.1:56379/0`.
Ative GPS, calcule/selecione a rota recomendada e inicie navegacao no browser.
Alternativas e itinerarios EV inteiros nao sao convertidos automaticamente
em sessoes de navegacao.

## Evidencias

Testes locais cobrem projecao fracionaria, ETA ponderada, duplicatas,
ordem, precisao, chegada, histerese/cooldown, falha e preferencias de recalc,
TTL, CAS, isolamento de proprietarios, Origin, HTTP/WS, reconnect e stop.
Browser com Geolocation/mapa sinteticos verificou progresso e stream,
reconexao com a mesma revisao e parada; nao GPS fisico/permissoes nativas.

Dois testes reais disponiveis: Redis CAS/TTL e duas instancias ASGI com
PostGIS/Redis compartilhados. Exigem `MAPINATOR_TEST_REDIS_URL` e, para
o segundo, `MAPINATOR_TEST_DATABASE_URL`, ambos descartaveis/isolados.
Ainda skipped neste host; Docker Linux indisponivel. Nao foi comprovado
failover, concorrencia de producao ou transporte HTTP/TLS real entre instancias.
