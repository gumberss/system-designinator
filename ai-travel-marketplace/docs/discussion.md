# Discussões de engenharia

Este documento registra perguntas técnicas discutidas durante a implementação
do AI Travel Marketplace, com respostas relacionadas ao código e à arquitetura
atuais. A intenção é preservar o raciocínio e servir como material de consulta
para decisões e conceitos explorados no projeto.

## AI Gateway e MCP

### Como o servidor MCP é integrado ao AI Gateway e quais ferramentas ele expõe?

O servidor MCP é criado por `create_mcp_server()` em
[`services/ai_gateway/assistant_mcp.py`](../services/ai_gateway/assistant_mcp.py).
Essa função registra as ferramentas de busca de viagens, criação e consulta de
itinerários, criação de cotações, consulta do estado de uma reserva e início de
booking.

O transporte Streamable HTTP é configurado em
[`services/ai_gateway/app.py`](../services/ai_gateway/app.py) e montado no
caminho interno `/mcp`. O Gateway encaminha chamadas externas para o AI
Gateway, tornando o endpoint acessível pela rota `/api/ai/mcp`. As requisições
MCP são autenticadas; operações de compra continuam sujeitas à confirmação
explícita do usuário e às validações do Booking Orchestrator.

Essa integração mantém o MCP como uma fronteira de ferramentas: ele encaminha
operações aos serviços responsáveis, sem substituir suas regras de negócio nem
acessar diretamente os bancos de dados transacionais.

### Qual é a responsabilidade de `_platform_request` nas ferramentas MCP?

`_platform_request`, em
[`services/ai_gateway/assistant_mcp.py`](../services/ai_gateway/assistant_mcp.py),
é uma função auxiliar para chamadas HTTP do AI Gateway aos serviços da
plataforma. Uma ferramenta informa o serviço, o método HTTP e o caminho; a
função resolve a URL configurada por variável de ambiente e pode incluir
parâmetros de consulta, corpo JSON e uma chave de idempotência.

Ela também propaga o contexto de autenticação do usuário, aplica um timeout e
transforma falhas de transporte, respostas HTTP de erro e respostas JSON
inválidas em erros explícitos. Não executa retries automáticos: isso evita
repetir implicitamente operações cujo resultado possa ser incerto. Quando a
operação tem efeitos colaterais, a idempotência e a recuperação devem ser
tratadas pelo fluxo de negócio apropriado, especialmente pelo Booking
Orchestrator.

### O que significa configurar `lifespan=lifespan` ao criar a aplicação FastAPI?

Na criação do FastAPI em
[`services/ai_gateway/app.py`](../services/ai_gateway/app.py),
`lifespan=lifespan` registra a função assíncrona `lifespan` como gerenciadora do
ciclo de vida da aplicação. Ela define o trabalho executado na inicialização,
durante a operação e no encerramento do serviço.

A função usa `yield` para separar essas etapas:

- **Inicialização, antes do `yield`:** configura o banco de dados e a fábrica
  de sessões, os clientes de OpenAI e Qdrant quando disponíveis, o servidor MCP
  e tarefas em segundo plano.
- **Aplicação em execução, no `yield`:** o FastAPI atende requisições enquanto
  esses recursos permanecem disponíveis.
- **Encerramento, após o `yield`:** cancela as tarefas, fecha os clientes HTTP,
  libera o engine do banco de dados e remove a aplicação MCP montada.

Centralizar essa gestão no lifespan alinha a criação e a liberação dos recursos
com o processo do serviço, em vez de recriá-los a cada requisição.

## Embeddings e busca vetorial

### O que representa `OPENAI_EMBEDDING_DIMENSIONS=1536` e por que usar esse valor?

`OPENAI_EMBEDDING_DIMENSIONS` configura quantos números compõem cada vetor
gerado pelo modelo de embeddings. No AI Gateway, o valor padrão `1536` é passado
à API de embeddings junto com o modelo `text-embedding-3-small`.

Esse valor foi escolhido porque corresponde à dimensão padrão desse modelo.
Os componentes do vetor representam características semânticas aprendidas pelo
modelo; não são palavras ou categorias com significado individual diretamente
interpretável. O Qdrant precisa criar a coleção com o mesmo número de dimensões
para armazenar e comparar esses vetores. Por isso, o AI Gateway também verifica
a configuração da coleção e o tamanho dos vetores retornados pelo provedor.

O modelo permite configurar uma dimensão menor. Isso pode reduzir o espaço
armazenado e o custo de busca, mas pode afetar a qualidade da recuperação e
exige avaliação. A dimensão configurada deve permanecer consistente entre
geração de embeddings, coleção Qdrant e consultas. Alterá-la em uma coleção já
existente não é uma simples mudança de configuração: em geral, exige criar ou
recriar a coleção e gerar novamente os embeddings do corpus.

## Ingestão de conhecimento para RAG

### O que `corpus` e `corpus_revision` fazem em `load_knowledge_chunks`?

Em `load_knowledge_chunks`, `corpus` não guarda os documentos: é um acumulador
SHA-256 usado para calcular uma impressão digital do conjunto de conhecimento.
A função percorre os arquivos Markdown em ordem determinística e adiciona ao
hash o nome e o conteúdo de cada arquivo. Ao final, `corpus.hexdigest()` produz
`corpus_revision`, uma string que identifica aquela versão do conjunto de
fontes.

Depois, a função divide os textos em chunks e inclui esse `corpus_revision` no
payload de cada chunk. Ela retorna a revisão junto com a lista de chunks.
Durante a ingestão, `_ingest_knowledge` consulta o Qdrant por pontos marcados
com aquela revisão; se a quantidade encontrada corresponder à quantidade de
chunks esperada, evita recalcular e reenviar todos os embeddings.

Assim, a revisão ajuda a detectar mudanças nas fontes e a evitar trabalho
repetido na inicialização. Ela é diferente do hash `revision` de cada
documento, que identifica individualmente o conteúdo de um arquivo. Uma
mudança em qualquer arquivo ou nome de fonte altera a impressão digital do
corpus inteiro.

## Persistência do AI Gateway

### Para que servem `build_engine(...)` e `AssistantBase.metadata.create_all(engine)`?

No lifespan do AI Gateway, estas linhas preparam o acesso ao banco de dados
usado pelo próprio serviço:

```python
engine = build_engine(os.getenv("DATABASE_URL", "sqlite:///./assistant.db"))
AssistantBase.metadata.create_all(engine)
```

`build_engine(...)` cria o SQLAlchemy Engine, que representa a configuração e o
ponto de acesso às conexões com o banco. O caminho vem da variável
`DATABASE_URL`; se ela não estiver definida, o serviço usa `assistant.db` em
SQLite, útil para execução local. No Compose, a configuração aponta para a
base PostgreSQL `assistant`.

`AssistantBase.metadata.create_all(engine)` lê os modelos ORM declarados sob
`AssistantBase` e cria no banco as tabelas que ainda não existem. Neste serviço,
isso inclui as tabelas de conversas, itinerários e cotações do assistente. Esse
método não atualiza automaticamente tabelas já existentes quando os modelos
mudam; para alterações de schema em ambientes evolutivos, deve-se usar
migrações versionadas, por exemplo com Alembic.

O Engine é criado uma vez durante a inicialização e, em seguida, usado para
construir a fábrica de sessões que atende operações do serviço. No encerramento
do lifespan, `engine.dispose()` libera as conexões gerenciadas por ele.

### O que faz `app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)`?

Essa linha cria uma fábrica de sessões SQLAlchemy vinculada ao Engine e a
guarda em `app.state`, onde handlers e ferramentas do AI Gateway podem
recuperá-la. Cada chamada a `session_factory()` cria uma nova `Session`, usada
para consultar ou alterar dados e normalmente encerrada com um bloco `with`.
Assim, o serviço reutiliza a configuração do Engine, mas não compartilha uma
mesma sessão mutável entre requisições.

`expire_on_commit=False` faz com que, após `session.commit()`, os atributos dos
objetos ORM carregados permaneçam disponíveis sem que o SQLAlchemy os expire e
precise buscá-los novamente do banco na próxima leitura. Isso é útil em fluxos
que confirmam a transação e depois ainda leem os objetos, por exemplo para
montar uma resposta.

Essa opção não mantém a sessão aberta nem altera o fato de que a transação foi
confirmada. A sessão ainda deve ter escopo curto e ser fechada corretamente;
atributos que não foram carregados antes do fechamento podem não estar
disponíveis para acesso posterior.

## Provedor de IA

### O que faz a construção de `OpenAIProvider` e qual é sua utilidade?

Durante o lifespan, o AI Gateway instancia
`OpenAIProvider(openai_client, chat_model=..., embedding_model=...,
embedding_dimensions=...)` quando existe uma chave de API configurada. O
objeto recebe o cliente HTTP já configurado para o endpoint OpenAI, além dos
nomes de modelo e da dimensão de embeddings. A instância é guardada em
`app.state.provider` para ser reutilizada pelos handlers.

O provider centraliza a comunicação com a API de IA. Seu método `embed`
solicita embeddings e valida a quantidade, a ordem e as dimensões dos vetores;
`complete` obtém uma resposta de chat; e `complete_with_tools` obtém respostas
que podem incluir chamadas de ferramentas. Assim, os handlers trabalham com
métodos e resultados definidos pelo projeto, enquanto detalhes HTTP e formato
da API ficam concentrados em
[`services/ai_gateway/provider.py`](../services/ai_gateway/provider.py).

Se `OPENAI_API_KEY` não estiver configurada, `app.state.provider` recebe
`None`. Isso permite iniciar os recursos não relacionados a IA sem credenciais;
os endpoints que precisam do modelo devem então responder como indisponíveis.
No estado atual, `OpenAIProvider` é uma implementação concreta para a API
OpenAI — a classe organiza essa integração e facilita testes, mas não significa,
por si só, que já exista suporte implementado a outros provedores.

### O que representa `app.state.runtime` e para que servem seus campos?

Durante a inicialização do AI Gateway, `app.state.runtime` recebe um
dicionário de estado em memória para acompanhar a preparação da base de
conhecimento usada pelo RAG:

```python
app.state.runtime = {
    "knowledge_ready": False,
    "knowledge_chunks": 0,
    "degraded_reason": None,
}
```

Os valores iniciais indicam que o índice ainda não foi preparado: ainda não há
chunks confirmados e nenhum motivo de degradação foi registrado.

- `knowledge_ready` indica se o AI Gateway conseguiu preparar ou verificar o
  índice de conhecimento no Qdrant.
- `knowledge_chunks` registra quantos chunks estão disponíveis no índice.
- `degraded_reason` guarda uma explicação operacional quando a preparação não
  pode ser concluída, por exemplo por falta de chave de API ou indisponibilidade
  do índice.

A tarefa `_prepare_knowledge` atualiza esses valores enquanto carrega e indexa
os documentos. O endpoint `/ready` usa o estado junto com a disponibilidade do
provider para decidir se o componente de IA está pronto; caso contrário,
responde com HTTP 503. Como `app.state` é memória local do processo, esse estado
é reiniciado a cada inicialização e não é um registro persistente ou uma
telemetria compartilhada entre réplicas.

## Transporte HTTP do MCP

### O que faz `streamable_http_app(...)` e como o dispatcher o publica?

`mcp_server.streamable_http_app(...)` cria uma aplicação ASGI que implementa o
transporte Streamable HTTP do MCP. Ela é a camada de transporte para clientes
MCP trocarem mensagens do protocolo com o servidor por HTTP; não cria as
ferramentas em si. As ferramentas são registradas antes, por
`create_mcp_server()`.

Neste serviço, `streamable_http_path="/"` define a rota raiz dentro da aplicação
de transporte. A aplicação principal monta um dispatcher em `/mcp`; por isso,
o caminho interno `/` é publicado como `/mcp` no AI Gateway. Através do Gateway
externo, a rota é `/api/ai/mcp`.

`TransportSecuritySettings(allowed_hosts=allowed_mcp_hosts)` restringe os
valores de `Host` aceitos pelo transporte, reduzindo riscos de acesso por
hosts inesperados, como em ataques de DNS rebinding. Os hosts permitidos vêm
da variável `MCP_ALLOWED_HOSTS`, com valores locais como padrão de
desenvolvimento.

Por fim, `mcp_dispatcher.set_app(mcp_http_app)` conecta a aplicação de
transporte ao dispatcher. O dispatcher encaminha as requisições recebidas em
`/mcp` para ela; antes dessa configuração, retorna HTTP 503 indicando que o MCP
está iniciando. No encerramento do lifespan, o delegate é removido para que o
dispatcher não continue apontando para a aplicação MCP já encerrada.

## Configuração da coleção vetorial

### O que significam `vectors.size` e `vectors.distance` na configuração do Qdrant?

A função `qdrant_collection_body(dimensions)` em
[`services/ai_gateway/provider.py`](../services/ai_gateway/provider.py) monta
parte do corpo enviado ao Qdrant para criar uma coleção vetorial:

```python
"vectors": {
    "size": dimensions,
    "distance": "Cosine",
}
```

- `size` define o número de componentes de cada vetor que será armazenado.
  Deve corresponder à dimensão retornada pelo modelo de embeddings configurado
  em `OPENAI_EMBEDDING_DIMENSIONS`; no padrão atual, são 1536.
- `distance` escolhe como o Qdrant compara vetores durante a busca. `Cosine`
  usa similaridade de cosseno: compara a orientação dos vetores, em vez de
  apenas sua distância absoluta. É uma métrica comum para embeddings semânticos.

Na prática, o Qdrant compara o embedding da pergunta aos embeddings dos
documentos e retorna os mais semelhantes segundo essa métrica. O tamanho deve
ser compatível entre documentos, consultas e coleção; a métrica também deve ser
mantida consistente com a estratégia adotada para esses embeddings. Mudar o
tamanho normalmente exige uma coleção compatível nova e reindexação dos
vetores.

### Como `load_knowledge_chunks` divide os documentos em chunks?

Depois de ler e identificar as fontes, a função processa cada documento
separadamente. O laço
`for source_id, title, text, revision in documents` recupera os metadados e o
texto de uma fonte; por isso, um chunk não mistura conteúdo de dois arquivos.

1. **Normaliza e separa parágrafos.** `text.split("\n\n")` divide o documento
   em blocos separados por linhas em branco. Para cada bloco,
   `re.sub(r"\s+", " ", part).strip()` transforma sequências de espaços,
   tabs e quebras de linha em um espaço e remove espaços das extremidades.
   Isso deixa o texto mais uniforme, mas significa que o chunking não preserva
   a formatação original do Markdown.
2. **Prepara o chunk em construção.** `current` começa vazio e acumula texto
   para o chunk atual. `part` é o número sequencial desse chunk dentro da
   fonte; ele é usado nos metadados e na identificação do ponto no Qdrant.
3. **Preenche respeitando o limite.** Para cada parágrafo, o `while paragraph`
   continua até consumir todo o texto. `available` calcula quanto espaço resta
   até `CHUNK_SIZE`, descontando também o espaço que será usado para juntar o
   próximo segmento a `current`.
4. **Escolhe o próximo segmento.** `paragraph[:available]` pega no máximo o
   texto que cabe. Se esse corte cair no meio de uma palavra, `rfind(" ")`
   procura o último espaço no segmento. Quando ele está depois da metade do
   espaço disponível, o corte recua até ali para evitar partir a palavra sem
   desperdiçar muito espaço. Se não houver um espaço adequado, mantém o corte
   pelo limite de caracteres.
5. **Avança o texto restante.** O segmento é anexado a `current`, separado por
   um espaço; depois, é removido do início de `paragraph`. O loop processa o
   restante na iteração seguinte.
6. **Fecha e sobrepõe chunks.** Quando `current` atinge o limite, ele é salvo
   por `_make_chunk`. `part` é incrementado e `current` passa a conter os
   últimos `CHUNK_OVERLAP` caracteres do chunk anterior. Essa sobreposição
   repete um pequeno contexto no chunk seguinte, reduzindo a chance de uma
   informação importante ficar separada exatamente na fronteira.
7. **Salva o restante.** Ao terminar o documento, qualquer texto ainda
   acumulado em `current` é salvo como o último chunk, mesmo que seja menor que
   o limite. Por fim, a função retorna a revisão do corpus e todos os chunks.

Com a configuração atual, `CHUNK_SIZE` é 900 e `CHUNK_OVERLAP` é 120. São
**caracteres**, não tokens do modelo. Portanto, chunks de mesmo tamanho podem
ter quantidades diferentes de tokens. A sobreposição também é calculada em
caracteres e pode, em um corte arbitrário, começar ou terminar no meio de uma
palavra.

O teste de ingestão verifica que os chunks gerados têm até 900 caracteres para
o conteúdo de teste. Ainda assim, o algoritmo usa um corte de contingência por
caracteres para trechos sem espaços; textos com sequências excepcionalmente
longas sem espaços merecem um teste de limite próprio se essa entrada passar a
ser esperada no corpus.

### O que faz a consulta de contagem de pontos no Qdrant durante a ingestão?

Em `_ingest_knowledge`, o serviço envia uma requisição HTTP `POST` ao endpoint
de contagem de pontos da coleção `travel-knowledge-v1`. O corpo pede uma
contagem exata (`"exact": True`) e filtra os pontos cujo payload tem
`corpus_revision` igual à revisão calculada por `load_knowledge_chunks`.

Isso responde: “quantos pontos desta versão específica do corpus já estão
armazenados no Qdrant?”. Após verificar se a resposta HTTP foi bem-sucedida,
o código lê `result.count`. Se essa contagem for igual ao número de chunks
gerados localmente, `_ingest_knowledge` assume que a revisão já está indexada e
retorna sem chamar novamente o modelo para gerar embeddings. Se as contagens
diferirem, continua pelo fluxo de embeddings e reindexação da coleção.

A comparação evita trabalho repetido quando a inicialização encontra a mesma
versão do conteúdo. Ela é uma verificação operacional simples, não uma auditoria
completa dos pontos: contar não confirma que cada ID esperado existe, que cada
vetor está correto ou que todos os payloads estão íntegros.

### Por que a contagem no Qdrant usa `POST` se a operação só consulta dados?

O verbo HTTP não é sempre suficiente para concluir se uma operação altera
dados. Nesse caso, o endpoint de contagem do Qdrant é chamado com `POST` porque
a API recebe no corpo JSON os parâmetros da consulta, incluindo o filtro e a
opção de contagem exata. Embora o método seja `POST`, a intenção dessa chamada é
somente ler/contar pontos: ela não cria, atualiza nem exclui dados.

Isso é diferente dos `POST` usados pelo AI Gateway para iniciar uma reserva ou
criar uma cotação, que representam comandos da aplicação e podem iniciar
operações de negócio. Para entender o efeito de uma chamada, considere em
conjunto o endpoint, o corpo enviado e o contrato da API, não apenas o verbo.

### Como funciona o fluxo completo de ingestão do corpus no Qdrant?

O entendimento geral está descrito em
[`docs/11-phase6-ai-foundation.md`](./11-phase6-ai-foundation.md), na seção
“Curated RAG”. O fluxo concreto de `_ingest_knowledge` é:

1. `load_knowledge_chunks` lê os arquivos Markdown, produz a revisão do corpus
   e divide os documentos em chunks.
2. O AI Gateway conta no Qdrant os pontos que têm essa revisão.
3. Se a contagem corresponder ao número de chunks esperado, a função considera
   que o corpus já está indexado e encerra sem pedir novos embeddings.
4. Se a coleção não tiver pontos dessa revisão, ou se a contagem for diferente
   da esperada, o serviço envia **todos os chunks** ao modelo de embeddings em
   lotes e valida a dimensão dos vetores retornados.
5. Depois da validação, exclui e recria a coleção `travel-knowledge-v1`.
   Insere os pontos em lotes; cada ponto contém um ID, o vetor no campo
   `vector` e os metadados/texto do chunk no campo `payload`.

Portanto, não é apenas um ramo “Qdrant vazio” versus “Qdrant preenchido”: a
decisão depende da contagem associada à revisão atual. Uma contagem diferente
provoca a reindexação completa da coleção, inclusive quando a causa for uma
indexação parcial. A comparação é simples e pode não detectar corrupção ou
substituição de pontos se a quantidade continuar igual.

Se `OPENAI_API_KEY` não estiver configurada, `_prepare_knowledge` marca o
serviço como degradado e não inicia a ingestão. Se uma chamada ao Qdrant ou ao
provedor falhar durante a preparação, registra o erro, mantém o conhecimento
como não pronto e tenta novamente após dez segundos. Como a coleção é excluída
antes de todos os novos pontos serem inseridos, uma falha durante a etapa de
recriação pode deixá-la temporariamente vazia ou parcial; a próxima tentativa
pode reconstruí-la, mas o código não faz uma troca atômica entre coleção antiga
e nova.
