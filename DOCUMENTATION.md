# Documentação Técnica: SearchAgent v2 (OpenAlex Engine)

O `SearchAgent v2` é o motor de descoberta de literatura e expansão de grafos bibliográficos que sustenta as capacidades de busca inteligente do **SciLaunch**. Ele é alimentado pela API pública da OpenAlex (~250M+ de registros acadêmicos).

---

## 1. Arquitetura do Agente

A classe `SearchAgent` expõe duas capacidades modulares e desacopladas:

1.  **Busca Textual de Alta Precisão (`search_by_keyword`)**: Realiza consultas baseadas em filtros restringindo os termos de busca ao título e resumo (`title_and_abstract.search`), reduzindo ruídos interdisciplinares.
2.  **Expansão Bibliográfica e Construção de Grafo (`build_citation_graph`)**: Constrói um grafo unificado direcionado (`networkx.DiGraph`) ligando múltiplos DOIs sementes (*seeds*) a seus respectivos trabalhos referenciados (expansão *backward*) e trabalhos citadores (expansão *forward*).

---

## 2. Decisões de Design e Otimizações de Rede

### A. O Bug Crítico de Referências (Nós *Backward*) Resolvido
*   **O Bug:** Na API do OpenAlex, a lista de referências de um artigo (`referenced_works`) retorna URLs contendo IDs canônicos no formato `https://openalex.org/W19621276`.
*   **O Impacto:** O script original realizava a requisição `requests.get` diretamente neste link. No entanto, o domínio `openalex.org` aponta para o site público HTML (que responde com status 200 OK contendo a interface web em Vue.js). Ao tentar ler esse HTML como JSON (`r.json()`), o script quebrava com `JSONDecodeError` e descartava a referência de forma silenciosa.
*   **A Correção:** O método `get_referenced_works` agora intercepta o ID canônico e converte o domínio público para o endpoint de API do OpenAlex (`api.openalex.org/works/`), garantindo que o retorno seja JSON autêntico.
*   **Tolerância a Falhas:** Cada chamada de referência individual está envelopada em um bloco `try...except`, garantindo que se uma referência específica falhar (ex: HTTP 404), as demais continuem a ser processadas.

### B. Cache de Memória Local para Otimização de Cota
*   **Problema de Redundância:** Em grafos de múltiplos seeds, artigos de uma mesma área científica frequentemente compartilham as mesmas referências (*co-citation*). Sem cache, o agente faria chamadas HTTP redundantes para a API para obter o mesmo trabalho repetidamente, desperdiçando a cota diária de API e adicionando latência desnecessária.
*   **A Implementação:** A classe gerencia um dicionário interno `self._works_cache` no escopo do objeto. Toda vez que um trabalho é baixado (via DOI, busca ou referências), ele é registrado no cache sob seu DOI e seu ID do OpenAlex. As chamadas subsequentes buscam os dados em memória RAM instantaneamente.

### C. Higienização Automática de Filtros
*   **Problema de Sintaxe:** O OpenAlex utiliza a vírgula (`,`) como delimitador para múltiplos filtros e o dois-pontos (`:`) para associar chave e valor. Se uma query de usuário contivesse esses caracteres (ex: `"resistance training, hypertrophy: structural"`), a API retornaria o erro `400 Bad Request`.
*   **A Implementação:** O método `search_by_keyword` higieniza a query automaticamente, substituindo esses caracteres especiais por espaços e eliminando espaços duplicados antes de montar a string do filtro de busca, evitando quebras inesperadas em tempo de execução.

---

## 3. Rastreabilidade de Dados e Proveniência (Auditoria do Pipeline)

*   **Identificação de Falhas:** O método `build_citation_graph` rastreia falhas estruturadas. Se uma das sementes falhar no processamento de rede, ela não quebra o script; ela é registrada na lista `G.graph["failed_seeds"]` anexada ao grafo retornado.
*   **Origem dos Dados:** O metadado `G.graph["source"]` é preenchido com `"api"` para dados reais e `"fallback_static"` para dados gerados a partir do fallback de testes, permitindo auditoria robusta da integridade de dados pelo RAG do SciLaunch.

---

## 4. Análise de Centralidade e Conectividade do Grafo

> [!WARNING]
> **PageRank sobre Grafos Desconectados**
> O cálculo de PageRank global (`nx.pagerank(G)`) sobre grafos gerados a partir de sementes independentes que não compartilham referências/citações é matematicamente esparso e resulta em múltiplos **componentes fracamente conectados** (*Weakly Connected Components*). Nesses casos, o PageRank global misturará probabilidades estacionárias de fluxo isoladas.
> 
> *   **Recomendação para o Pipeline:** O pipeline superior do SciLaunch deve checar `nx.number_weakly_connected_components(G)`. Se a quantidade de componentes for maior do que 1, as análises de centralidade de PageRank devem ser interpretadas separadamente por componente, ou sementes adicionais devem ser incluídas para criar pontes estruturais de co-citação.
