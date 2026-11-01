# short-term-rental: reservas concorrentes em C#

Laboratorio educacional inspirado no **tipo de problema** de Airbnb/Booking.com.
Nao e uma descricao da arquitetura interna dessas empresas.

O objetivo e entender uma garantia: **um imovel nao pode ter duas reservas
confirmadas com noites sobrepostas**, mesmo com requests simultaneos, varias
instancias da API e retries de rede.

**100M leituras/s e 10M escritas/s sao metas hipoteticas de arquitetura, nao
throughput prometido por este projeto.** O simulador mede sua maquina e prova
invariantes locais; nao simula magicamente a capacidade de um sistema global.

## O que foi implementado

- C# / .NET 10 / ASP.NET Core Minimal API.
- PostgreSQL 17: fonte de verdade, transacoes, admissao por imovel e constraint de exclusao.
- Redis: cache do catalogo, **nao** lock nem autorizacao de reservas.
- RabbitMQ: eventos persistentes com publisher confirms, ACK manual e dead-letter.
- Transactional outbox + inbox idempotente + projecao de estatisticas.
- Idempotency-Key para retries; cancelamento idempotente.
- Limitadores locais de concorrencia (backpressure, nao garantia anti-overbooking).
- Simulador C#: disputa, retries, SQL direto, limites dos intervalos, eventos e carga.
- Perfil opcional com duas APIs compartilhando o mesmo banco.

Nenhum recurso AWS/Azure e criado. Codigo e configuracoes estao neste repositorio.
O Docker armazena imagens/volumes em sua area local gerenciada; isso e inerente ao
Docker, nao provisionamento de cloud.

## Pasta e nomes do projeto

Todo o conteudo do laboratorio esta na subpasta `short-term-rental`.
Abra esta pasta como workspace no VS Code para usar as tasks de `.vscode/tasks.json`.
Execute os comandos deste README a partir dela, nao da pasta pai:

```powershell
Set-Location .\short-term-rental
```

A solucao e `short-term-rental.sln`. Projetos, assemblies e namespaces C# usam
`ShortTermRental.Api`, `ShortTermRental.Simulator` e `ShortTermRental.Tests`,
pois hifens nao sao validos em identificadores C#.
As variaveis do simulador agora usam o prefixo `SHORT_TERM_RENTAL_`.

O projeto Docker Compose, o banco e os usuarios locais agora se chamam
`short-term-rental`. Isso cria volumes novos; nao migra automaticamente os dados
do ambiente anterior. Containers e volumes anteriores foram preservados.
Se o ambiente anterior estiver rodando, pare seus containers antes de subir
este Compose: as portas locais continuam iguais e nao podem ser usadas pelos
dois ambientes ao mesmo tempo. Nao use `down -v` para essa troca.
Para recuperar dados anteriores, planeje backup/restauracao ou uma migracao
explicita; mudar variaveis de inicializacao nao renomeia usuarios/bancos em
volumes ja inicializados.

## Leia nesta ordem

1. [Arquitetura e escolha das ferramentas](docs/01-arquitetura.md)
2. [Modelagem de dados e indices](docs/02-modelagem-de-dados.md)
3. [Controle de concorrencia, passo a passo](docs/03-concorrencia.md)
4. [Outbox, mensageria, falhas e duplicatas](docs/04-eventos-e-falhas.md)
5. [Como pensar em 100M/10M requests por segundo](docs/05-escala-global.md)
6. [Experimentos, metricas e operacao](docs/06-experimentos-e-operacao.md)
7. [Resultados locais realmente verificados](docs/07-resultados-verificados.md)
8. [Indice GiST, extensao btree_gist e regra de exclusao](docs/08-indice-gist-e-exclusao.md)
9. [Locks do PostgreSQL: modos, conflitos e politicas de espera](docs/09-locks-do-postgresql.md)
10. [Por que outbox e quando retry duravel pode substitui-la](docs/10-outbox-e-retry-duravel.md)
11. [Outbox: UPDATE em lote, confirms e sucessos parciais](docs/11-outbox-lotes-e-sucessos-parciais.md)

## Requisitos e recursos que precisam ser criados

Para o laboratorio: Docker Desktop com **Linux containers** e Docker Compose v2.
SDK .NET 10 para executar o simulador/testes no host. A API pode rodar so no Docker.
Recomendacao inicial: 4 CPUs e 6-8 GB de RAM disponiveis; isso nao e requisito de
capacidade para os RPS hipoteticos. Internet no primeiro restore/build/pull.

O Compose cria localmente:

| Recurso | Como e criado | Persistencia |
|---|---|---|
| PostgreSQL + banco `short-term-rental` | imagem e variaveis do Compose | volume `postgres-data` |
| Schema, extensao `btree_gist`, 2000 imoveis | SQL de inicializacao | mesmo volume |
| Redis | imagem do Compose | cache descartavel, limite 128 MB |
| RabbitMQ + usuario de laboratorio | imagem e variaveis | volume `rabbitmq-data` |
| Exchanges, filas e DLQ | workers da API, declaracao idempotente | broker |
| API | build do Dockerfile | sem estado local |
| Segunda API | perfil `multi-instance` | sem estado local |

As senhas `local-development-only` sao **credenciais publicas de laboratorio**.
Nao publique isso na Internet: nao ha autenticacao, autorizacao, TLS, pagamentos
nem protecao contra abuso. Portas publicadas somente em `127.0.0.1`.

## Subir tudo

No PowerShell, a partir da raiz deste repositorio:

```powershell
docker compose up -d --build
docker compose ps
Invoke-RestMethod http://localhost:58080/health/ready
dotnet build short-term-rental.sln -c Release
dotnet test tests\ShortTermRental.Tests\ShortTermRental.Tests.csproj -c Release --no-build
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- verify
```

Resultado esperado: linhas `PASS`, exatamente **1 reserva confirmada e 199
conflitos** na disputa padrao, e **zero pares sobrepostos** no diagnostico.
O verificador cancela apenas as reservas que ele criou. O historico, outbox,
inbox e uma mensagem propositalmente invalida na DLQ ficam para inspecao.

O verificador usa HTTP **e tambem** PostgreSQL/RabbitMQ diretamente. `load`
consulta o banco no setup para escolher datas alem do historico existente,
sem incluir essa consulta nos RPS medidos. Por padrao,
as conexoes diretas apontam para as portas locais abaixo. Para alterar:
`SHORT_TERM_RENTAL_POSTGRES`, `SHORT_TERM_RENTAL_RABBIT_HOST`, `SHORT_TERM_RENTAL_RABBIT_PORT`,
`SHORT_TERM_RENTAL_RABBIT_USER`, `SHORT_TERM_RENTAL_RABBIT_PASSWORD`.
Nao aponte essas variaveis para producao. As duas URLs precisam usar **o mesmo banco**.

### Provar concorrencia entre duas instancias

```powershell
docker compose --profile multi-instance up -d --build
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- verify --second-url http://localhost:58081
```

A disputa e os retries alternam entre processos diferentes. Nao e um teste com
dois bancos ou duas regioes; ambos compartilham a mesma autoridade PostgreSQL.

### Medir carga local

```powershell
# Catalogo com cache: proporcao em numero de tentativas, nao RPS garantidos.
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 20 --concurrency 32 --read-percent 90 --hotspot-percent 10 --read-mode catalog

# Disponibilidade dinamica, sem cache: compara o custo da consulta no banco.
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 20 --concurrency 32 --read-percent 90 --hotspot-percent 10 --read-mode availability

# So escrita, sem hotspot intencional; imoveis/datas diferentes.
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 20 --concurrency 32 --read-percent 0 --hotspot-percent 0
```

`load` deixa as reservas criadas no banco e usa uma nova janela depois do maior
checkout do historico para nao conflitar acidentalmente com cargas anteriores.
`verify` usa uma janela futura aleatoria.
Nao execute verificadores/cargas simultaneos durante as assercoes de projecao.
Relatorios podem ser salvos **aqui**, por exemplo:

```powershell
New-Item -ItemType Directory -Force artifacts | Out-Null
dotnet run --project src\ShortTermRental.Simulator -c Release --no-build -- load --duration 20 > artifacts\carga-local.json
```

O relatorio distingue tentativas/s, reservas realmente confirmadas/s, HTTP
200/201/409/429, erros de transporte e latencias p50/p95/p99 combinadas.
Erros inesperados ou overlaps fazem o processo terminar com codigo diferente de zero.

O pool PostgreSQL esta limitado a 50 conexoes por API (incluindo seus workers).
No perfil com duas APIs, o teto agregado e 100, ainda sem margem garantida
para outros clientes do PostgreSQL. `load` aceita ate 256 workers para medir
concorrencia HTTP; esse numero nao altera os pools ou limites da API.

### Testes com k6

Tambem ha cenarios HTTP em [scripts/k6](scripts/k6/README.md): carga de leitura,
escrita/mista, disputa com um unico vencedor e retries idempotentes.
O guia inclui comandos PowerShell com k6 ou Docker, thresholds, limpeza e os
smoke tests executados. Os cenarios complementam, nao substituem, o verificador
C# que acessa banco/mensageria e verifica convergencia.

### Enderecos

| Servico | Endereco no host |
|---|---|
| API | http://localhost:58080 |
| Segunda API | http://localhost:58081 |
| RabbitMQ management | http://localhost:55673 |
| PostgreSQL | localhost:55432 |
| Redis | localhost:56379 |

### Fazer uma reserva manual

```powershell
$property = (Invoke-RestMethod 'http://localhost:58080/properties?limit=1')[0]
$key = [guid]::NewGuid().ToString()
$body = @{
  propertyId = $property.id
  guestId = [guid]::NewGuid().ToString()
  checkIn = '2035-01-10'
  checkOut = '2035-01-15'
} | ConvertTo-Json
$booking = Invoke-RestMethod -Method Post -Uri 'http://localhost:58080/bookings' -ContentType 'application/json' -Headers @{'Idempotency-Key' = $key} -Body $body
# Mesmo corpo + mesma chave: retorna a mesma reserva, nao cria outra.
Invoke-RestMethod -Method Post -Uri 'http://localhost:58080/bookings' -ContentType 'application/json' -Headers @{'Idempotency-Key' = $key} -Body $body
Invoke-RestMethod -Method Post -Uri "http://localhost:58080/bookings/$($booking.id)/cancel"
Invoke-RestMethod http://localhost:58080/diagnostics
```

Se outra reserva ja ocupar essas datas, o POST retorna 409, corretamente.
Use outra janela livre para repetir o exemplo.

## Endpoints

| Metodo / caminho | Semantica |
|---|---|
| `GET /properties?limit=100&offset=0` | catalogo paginado, banco, maximo 1000 |
| `GET /properties/{id}` | catalogo cache-aside |
| `GET /properties/{id}/availability?checkIn=...&checkOut=...` | snapshot no primary; nao promete reserva futura |
| `POST /bookings` | chave UUID obrigatoria; 201 criada, 200 replay, 409 conflito |
| `GET /bookings/{id}` | estado atual no primary |
| `POST /bookings/{id}/cancel` | cancela ou retorna estado ja cancelado |
| `GET /diagnostics` | overlaps + contagens de banco/outbox/projecao |
| `GET /health/live` | processo vivo |
| `GET /health/ready` | PostgreSQL acessivel; nao garante Redis/broker saudaveis |

## API no host em vez de container

```powershell
docker compose up -d postgres redis rabbitmq
dotnet run --project src\ShortTermRental.Api --no-launch-profile --urls http://localhost:58080
```

Nao rode API do host e container na mesma porta. As configuracoes padrao de
desenvolvimento ja apontam para a infraestrutura local.

## Parar e diagnosticar

Teste automatizado de falhas de runtime, **somente com este laboratorio ocioso**:

```powershell
powershell -NoProfile -File scripts\Test-Failures.ps1
```

O script para/reinicia PostgreSQL, Redis e RabbitMQ deste Compose, comprova
resposta 503 sem primary e convergencia dos eventos depois da recuperacao.
Restaura os servicos e cancela a reserva criada em `finally`.

```powershell
docker compose --profile multi-instance logs --tail 100 api api2
docker compose logs --tail 100 postgres rabbitmq
docker compose --profile multi-instance down
```

`down` preserva os volumes. SQL em `docker-entrypoint-initdb.d` roda apenas no
primeiro boot com volume vazio; editar SQL nao migra um banco existente.
Para evoluir dados reais, use migrations versionadas. Nao apague volumes para
"atualizar" um sistema real. Veja detalhes e exercicios nos documentos.
