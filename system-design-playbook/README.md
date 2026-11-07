# Guia reutilizavel para novos system designs

Use este documento como briefing, checklist e prompt para criar outro
laboratorio neste repositorio. O objetivo e aprender **por que** uma escolha
funciona, implementar o caminho critico e provar invariantes, nao acumular
tecnologias em um diagrama.

## Prompt para copiar e adaptar

```text
Crie um novo system design sobre [DOMINIO / PRODUTO], em uma nova pasta
[NOME-KEBAB-CASE] na raiz deste repositorio.

Use short-term-rental e ride-hailing apenas como referencias de profundidade
e organizacao. NAO altere nenhum laboratorio existente.

Quero aprender a desenhar e executar o sistema, nao so receber um diagrama.
Linguagem preferida: [LINGUAGEM, ex. TypeScript].
Fluxos obrigatorios: [LISTAR EXPERIENCIAS DO USUARIO].
Invariantes criticas: [LISTAR O QUE NUNCA PODE ACONTECER].
Carga hipotetica: [USUARIOS, RPS, EVENTOS/S, GEOGRAFIA OU EXPLICITAR HIPOTESES].
Restricoes: [RECURSOS LOCAIS, LATENCIA, CONSISTENCIA, CUSTO].

1. Examine as referencias e declare escopo, hipoteses e nao objetivos.
   Pergunte sobre ambiguidades que mudam materialmente a implementacao.
2. Escolha um nome apropriado e mantenha tudo isolado na nova pasta:
   codigo, schema, testes, Compose, portas, volumes e documentacao.
3. Implemente um laboratorio local executavel do caminho critico.
   Use os bancos e tecnologias que atendem cada necessidade; mais de um
   banco e permitido, nao obrigatorio. Justifique a complexidade.
4. Mostre o fluxo de ponta a ponta. Se a experiencia depende de movimento,
   progresso ou tempo real, inclua simulador e visualizacao local apropriada.
5. Documente cada decisao importante com problema, escolha, alternativas,
   custos, limites e condicoes que fariam voce mudar de ideia.
6. Inclua diagramas Mermaid de arquitetura, dados, sequencia e estados
   quando aplicaveis. Diagramas devem corresponder ao codigo executavel.
7. Explique concorrencia, atomicidade, idempotencia, ordenacao, falhas,
   retries, expiracao, backpressure, observabilidade e recuperacao.
8. Adicione algumas funcionalidades que mudem o desenho e ensinem conceitos.
   Separe as efetivamente implementadas das extensoes somente propostas.
9. Desenhe a evolucao de escala, incluindo dimensionamento com contas,
   hotspots, particionamento, retencao, custos e compromissos de consistencia.
10. Pode mencionar servicos cloud como alternativas, mas NAO crie recursos,
    nao faca deploy cloud e nao exija credenciais cloud para o laboratorio.
11. Valide build/tipos, testes unitarios e integracao real nas garantias que
    dependem de banco/broker. Quando relevante, use duas instancias.
12. Documente resultados realmente verificados, ambiente e comandos.
    Nao invente benchmarks, nao confunda meta hipotetica com capacidade medida
    e nao esconda tentativas que falharam.
13. Entregue README com inicio rapido, indice de leitura, contrato HTTP,
    recursos criados localmente, limites e como parar sem apagar dados.

Nao use locks apenas em memoria para garantias que precisam valer entre APIs.
Nao prometa exactly-once sem demonstrar os limites de cada fronteira.
Nao publique endpoints de laboratorio como se estivessem prontos para producao.
Nao exponha segredos, nao altere volumes/projetos alheios e nao remova dados
para fazer um teste parecer bem-sucedido.
```

## Estrutura sugerida

```text
novo-dominio/
  README.md
  compose.yaml
  docs/
    01-arquitetura.md
    02-modelagem-de-dados.md
    03-concorrencia.md
    04-eventos-e-falhas.md
    05-escala.md
    06-experimentos-e-operacao.md
    07-resultados-verificados.md
  infra/
  src/
  tests/ ou src/tests/
  scripts/ ou simulador em src/
```

Ajuste nomes/quantidade ao problema. Nao criar arquivos vazios apenas para
cumprir estrutura, nem microservicos sem necessidade.

## Checklist de raciocinio

### Problema antes de tecnologia

- Quem escreve, quem le e quem pode decidir?
- Qual e a unidade disputada: imovel/noite, motorista, saldo, assento, estoque?
- A garantia precisa valer entre processos, regioes ou somente em uma maquina?
- O usuario precisa de confirmacao imediata ou pode aguardar processamento?
- Qual dado e autoritativo e qual e projecao/cache?

### Escrita e concorrencia

- Localize o **ponto de linearizacao**: em que commit/CAS a decisao se torna real?
- O storage impede o erro mesmo se outra API ou SQL direto tentar?
- Locks tem escopo, ordem, deadline e custo claros?
- Retry da mesma intencao retorna o mesmo recurso? Reuso errado da chave falha?
- Estado terminal pode ressuscitar por retry, evento antigo ou timeout?
- Pagamento/efeito externo tem chave propria e reconciliacao?

### Eventos e tempo

- Quem define ordem: versao da entidade, sequencia de produtor ou offset do log?
- O cursor respeita commit ou pode pular transacoes atrasadas?
- Existe intervalo entre persistir e emitir que perde evento?
- Duplicatas e sucessos parciais sao tratados por entidade/efeito?
- Expiracao vem de qual relogio? Quem aplica fencing contra produtor antigo?
- Cliente lento/reconectado tem replay, snapshot e limite de memoria?

### Operacao e escala

- Conexoes, filas, memoria e armazenamento sao limitados?
- Pools somados cabem no banco? O sistema degrada com erro explicito?
- Particao/geografia resolve o hotspot ou apenas o desloca?
- Qual o comportamento sem cache, broker, banco ou uma regiao inteira?
- Ha politica de retencao, backup, restore e privacidade?
- SLO, RPO e RTO estao marcados como propostos ou medidos?

## Formato de ADR

| Campo | Conteudo esperado |
|---|---|
| Contexto | requisito e problema concreto |
| Decisao | tecnologia/protocolo e garantia oferecida |
| Alternativas | pelo menos uma opcao plausivel e por que nao agora |
| Trade-offs | latencia, consistencia, custo, complexidade, operacao |
| Limites | o que a decisao nao garante |
| Validacao | experimento que demonstra a garantia |
| Revisitar quando | volume/regra/risco que muda a escolha |

## Resultado honesto

Mantenha tres secoes separadas:

1. **Implementado:** codigo e infraestrutura locais existentes.
2. **Verificado:** comandos executados, resultados, ambiente e evidencias.
3. **Proposto:** evolucao de producao/escala, ainda nao implementada.

Para benchmarks, incluir duracao, concorrencia real, dataset, recursos,
throughput de tentativas e de sucessos, p50/p95/p99 e erros por tipo.
Zero violacoes de integridade nao significa capacidade suficiente.
Um teste mockado nao prova locks reais; uma API 200 nao prova efeito persistido.

## Referencias deste repositorio

- [Short-term rental](../short-term-rental/README.md): inventario por intervalo,
  exclusao PostgreSQL, idempotencia, outbox/inbox e experimentos de carga.
- [Ride-hailing](../ride-hailing/README.md): geolocalizacao, matching atomico,
  versao de estado, GPS sequenciado e replay SSE.
