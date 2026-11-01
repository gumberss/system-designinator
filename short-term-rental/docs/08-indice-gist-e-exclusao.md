# 8. Indice GiST: o que e e por que usamos nas reservas

Este documento detalha o indice que apoia a regra contra overbooking em
[001-schema.sql](../infra/postgres/001-schema.sql). Para o contexto das tabelas,
veja [modelagem de dados](02-modelagem-de-dados.md); para transacoes e disputa,
veja [concorrencia](03-concorrencia.md).

## Primeiro: o que e um indice?

Um indice e uma estrutura auxiliar que o banco mantem para encontrar dados
sem precisar, em todos os casos, percorrer a tabela inteira.

Pense no indice de um livro: em vez de ler todas as paginas para encontrar
um assunto, voce consulta uma estrutura que aponta para as paginas relevantes.
No banco, essa estrutura tambem precisa ser atualizada quando os dados mudam.

Um indice nao e uma copia gratuita: ocupa disco/memoria e custa trabalho em
INSERT, UPDATE, DELETE e manutencao. Sua existencia tambem nao obriga o
otimizador a utiliza-lo em toda consulta.

## B-tree e GiST nao resolvem exatamente o mesmo problema

**B-tree** e o tipo padrao de indice no PostgreSQL. Organiza valores de maneira
ordenada e funciona muito bem para igualdade, ordenacao e comparacoes de faixa:

```sql
WHERE id = '00000000-0000-0000-0000-000000000001'
WHERE created_at >= '2035-01-01'
```

**GiST** significa *Generalized Search Tree*: arvore de busca generalizada.
E uma infraestrutura extensivel de indexacao, nao apenas "um B-tree mais rapido".
O comportamento depende das classes de operadores dos tipos indexados.
No PostgreSQL, pode apoiar buscas como:

- intervalos que se sobrepoem;
- intervalos que contem um valor;
- relacoes geometricas e buscas por proximidade, conforme tipo/extensao.

| Pergunta | Indice adequado neste projeto |
|---|---|
| Qual reserva tem este bookingId? | B-tree da chave primaria |
| Essa chave de idempotencia ja existe? | B-tree PK/UNIQUE |
| Quais reservas desse imovel aparecem primeiro no historico? | B-tree de imovel + created_at |
| Ha estadias do mesmo imovel que se sobrepoem a essas datas? | GiST de imovel + intervalo |

B-tree pode ajudar consultas com datas; ele nao e incapaz de trabalhar com
intervalos. A diferenca aqui e o suporte direto do GiST ao operador de
**sobreposicao**, combinado com uma constraint de exclusao.

## A pergunta de negocio nao e "as datas sao iguais?"

Uma reserva existente de 10 a 15 de janeiro ocupa as noites 10, 11, 12, 13 e 14.
Outra reserva de 13 a 18 de janeiro tem datas diferentes, mas compartilha
as noites 13 e 14:

```text
Existente: [10 -------- 15)
Tentativa:       [13 -------- 18)
                  ^ noites compartilhadas
```

Uma regra como `UNIQUE(property_id, check_in, check_out)` impediria somente
duplicatas com os mesmos limites. Ela **nao impediria** esse overlap parcial.

Por isso modelamos uma estadia como um intervalo:

```sql
stay daterange GENERATED ALWAYS AS (
    daterange(check_in, check_out, '[)')
) STORED
```

- `daterange`: tipo nativo de intervalo de datas.
- `GENERATED ALWAYS`: o banco calcula a coluna a partir das datas.
- `STORED`: o resultado calculado e armazenado.
- `'[)'`: inclui check-in e exclui check-out.

Excluir o checkout permite uma reserva terminar no mesmo dia em que outra
comeca, sem compartilhar uma noite.

## O operador `&&`: os intervalos se sobrepoem?

```sql
SELECT daterange('2035-01-10', '2035-01-15', '[)')
    && daterange('2035-01-13', '2035-01-18', '[)');
-- true: compartilham noites

SELECT daterange('2035-01-10', '2035-01-15', '[)')
    && daterange('2035-01-15', '2035-01-18', '[)');
-- false: intervalos adjacentes, sem noites compartilhadas
```

GiST com a classe de operadores de ranges permite procurar candidatos a esse
tipo de conflito. Simplificando, a arvore guarda resumos das entradas e
descarta ramos que nao podem corresponder a busca. Conforme operador/plano,
podem ser necessarias verificacoes adicionais dos candidatos.

Nao significa tempo constante, ausencia de locks ou custo zero. A eficiencia
depende de distribuicao dos dados, seletividade, tamanho do indice e contencao.

## Por que instalar `btree_gist`?

Nossa busca combina dois tipos de comparacao:

```text
Mesmo imovel:           property_id = outro_property_id
Noites compartilhadas: stay && outra_stay
```

O PostgreSQL ja oferece suporte GiST para ranges. A extensao `btree_gist`
fornece classes de operadores GiST com comparacoes semelhantes as de B-tree
para outros tipos, incluindo **UUID**, usado em `property_id`.

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
```

Esse comando habilita a extensao no banco atual; `IF NOT EXISTS` evita erro
se ela ja estiver instalada. Nao instala um servico externo nem cria recursos
na AWS. O usuario precisa ter as permissoes exigidas pelo banco.

O nome nao significa "usar dois indices separados" ou converter todos os
B-trees em GiST. Aqui temos um **indice GiST composto** que consegue comparar
UUID e intervalo usando suas respectivas classes de operadores.

## A regra completa: `EXCLUDE USING gist`

O schema declara:

```sql
CONSTRAINT no_overlapping_confirmed_bookings
    EXCLUDE USING gist (
        property_id WITH =,
        stay WITH &&
    )
    WHERE (status = 'confirmed')
```

Leia assim:

> Nao podem existir duas linhas confirmadas nas quais o imovel seja igual
> e as estadias se sobreponham.

As duas comparacoes precisam ser verdadeiras para haver conflito:

| Mesmo imovel? | Intervalos sobrepostos? | Ambos confirmados? | Resultado |
|---|---|---|---|
| Sim | Sim | Sim | Rejeitar |
| Sim | Nao | Sim | Permitir |
| Nao | Sim | Sim | Permitir |
| Sim | Sim | Nao | Nao conflita com essa regra |

As colunas `property_id` e `stay` nao ficam nulas neste modelo: property_id
tem NOT NULL e stay e gerado de datas NOT NULL validadas por CHECK. Isso
evita que valores ausentes escapem da comparacao de conflito.

### Quem cria o indice?

A propria constraint de exclusao cria automaticamente seu indice de apoio
GiST. Nao precisamos adicionar outro `CREATE INDEX ... USING gist` identico.

O filtro `WHERE (status = 'confirmed')` torna esse indice **parcial**:
somente reservas confirmadas participam da regra de exclusao.

Depois que o cancelamento comita, a reserva deixa de ocupar o inventario
protegido por essa regra, mas permanece na tabela para historico. Entradas
fisicas antigas do indice seguem o ciclo normal de visibilidade/manutencao
do PostgreSQL; nao se deve imaginar uma limpeza completa instantanea do disco.

## Indice, constraint e lock: tres papeis diferentes

| Componente | Responsabilidade |
|---|---|
| GiST | Estrutura de acesso que apoia a busca/verificacao de conflitos |
| `EXCLUDE` | Regra de integridade que proibe reservas conflitantes |
| `btree_gist` | Suporte a comparacao do UUID dentro do GiST |
| `FOR NO KEY UPDATE` no imovel | Admissao pessimista das novas reservas pela API |
| Transacao | Commit/rollback conjunto de request, reserva e evento outbox |

Para comparar os modos de lock, suas matrizes de conflito e NOWAIT/SKIP LOCKED,
veja [locks do PostgreSQL](09-locks-do-postgresql.md).

**Criar apenas um indice GiST nao impede overbooking.** Um indice comum acelera
buscas; e a constraint EXCLUDE que estabelece a proibicao e faz o banco
verifica-la nas escritas.

Uma consulta previa dizendo "livre" tambem nao basta: outro guest pode reservar
antes do INSERT. A constraint participa do controle de concorrencia do banco,
inclusive quando ha transacoes conflitantes ainda em andamento.

Na API final, novas reservas adquirem lock na linha do imovel antes de inserir.
Isso reduz a disputa no indice quando muitos guests tentam reservar a mesma
acomodacao. A constraint permanece como defesa final para escritores que
ignorem essa admissao, inclusive um INSERT SQL direto.

Se o banco detectar overlap, retorna SQLSTATE **23P01**
(*exclusion_violation*). A API transforma esse conflito em HTTP **409**;
nao confirma a reserva. O rollback tambem desfaz request e outbox dessa tentativa.

## Por que foi escolhido nesta arquitetura?

1. **Expressa exatamente a regra:** mesmo imovel + noites sobrepostas.
2. **Cobre overlaps parciais:** nao depende de check-ins/check-outs identicos.
3. **Mantem uma linha por estadia:** nao precisamos criar uma linha por noite.
4. **Protege todas as instancias:** a autoridade e o PostgreSQL, nao memoria C#.
5. **Defende contra SQL direto:** a regra nao depende de passar pela API.
6. **Apoia a consulta de disponibilidade:** usamos o mesmo operador de overlap.
7. **Torna o problema didatico:** tipos de intervalo e integridade ficam
   explicitos no SQL, sem esconder a regra em um lock de Redis.

A consulta de disponibilidade em [BookingStore.cs](../src/ShortTermRental.Api/BookingStore.cs)
usa este formato:

```sql
SELECT NOT EXISTS (
    SELECT 1
    FROM bookings
    WHERE property_id = @property
      AND status = 'confirmed'
      AND stay && daterange(@start, @end, '[)')
)
FROM properties
WHERE id = @property;
```

O predicado `status = 'confirmed'` e compativel com o filtro do indice parcial.
O otimizador pode usar o GiST para localizar reservas relevantes, mas pode
preferir outro plano, por exemplo em uma tabela pequena. A integridade da
constraint nao depende do plano escolhido para esse SELECT.

## Como inspecionar sem alterar dados

No psql do laboratorio:

```sql
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public' AND tablename = 'bookings';
```

Voce deve encontrar o indice associado a
`no_overlapping_confirmed_bookings`, com `USING gist` e filtro de status,
alem dos indices B-tree.

Para estudar o plano, substitua o UUID por um imovel real do seed:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT id
FROM bookings
WHERE property_id = '00000000-0000-0000-0000-000000000001'
  AND status = 'confirmed'
  AND stay && daterange('2035-01-10', '2035-01-15', '[)');
```

`ANALYZE` executa a consulta e mede seu trabalho; aqui e um SELECT, sem alteracao
de dados. Procure o tipo de scan, nome do indice, linhas reais e buffers.
Nao conclua capacidade de producao com um UUID inexistente ou um dataset vazio.
Nao force index scan apenas para obter o plano que voce queria ver.

## Custos, limites e alternativas

- Inserir/cancelar reservas tambem custa manutencao de indice, WAL e locks.
- Muitas tentativas para o mesmo imovel continuam sendo um hotspot.
- EXCLUDE nao oferece fairness entre guests nem dispensa tratar abortos.
- Essa constraint vale neste banco; nao e uma regra global entre shards.
- GiST nao entrega, sozinho, 10M confirmacoes/s ou alta disponibilidade.
- Um imovel com capacidade N exige outro modelo, nao simplesmente remover EXCLUDE.

Alternativa: inventario diario com `UNIQUE(property_id, night)`. Nesse modelo,
reservar cinco noites cria cinco linhas dentro de uma unica transacao, que
precisa abortar inteira se alguma noite estiver ocupada. Pode ser adequado
para disponibilidade/capacidade por dia, mas aumenta escritas por estadia.

Escolhemos GiST + intervalos para ensinar exclusividade de uma unidade por
estadia. A escolha de producao dependeria do workload e da modelagem, nao de
uma regra universal de que GiST e melhor que B-tree.

## Resumo

> GiST ajuda a encontrar relacoes como sobreposicao. `btree_gist` permite
> combinar o UUID do imovel com esse intervalo. EXCLUDE usa essa estrutura
> para proibir duas reservas confirmadas que ocupem as mesmas noites.

Referencias oficiais:

- [GiST no PostgreSQL 17](https://www.postgresql.org/docs/17/gist.html)
- [Tipos de intervalo e constraints de exclusao](https://www.postgresql.org/docs/17/rangetypes.html)
- [Exclusion constraints](https://www.postgresql.org/docs/17/ddl-constraints.html#DDL-CONSTRAINTS-EXCLUSION)
- [Extensao btree_gist](https://www.postgresql.org/docs/17/btree-gist.html)
- [Indices parciais](https://www.postgresql.org/docs/17/indexes-partial.html)
