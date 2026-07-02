# Documentação Técnica: SearchAgent v2 (OpenAlex Engine)

O `SearchAgent v2` é o motor de descoberta de literatura e expansão de grafos bibliográficos que sustenta as capacidades de busca inteligente do **SciLaunch**. Ele é alimentado pela API pública da OpenAlex (~250M+ de registros acadêmicos).

---

## 1. Arquitetura do Agente

A classe `SearchAgent` expõe duas capacidades modulares e desacopladas:

1.  **Busca Textual de Alta Precisão (`search_by_keyword`)**: Realiza consultas baseadas em filtros restringindo os termos de busca ao título e resumo (`title_and_abstract.search`), reduzindo ruídos interdisciplinares. A query é higienizada automaticamente antes do envio para evitar quebras de sintaxe por vírgulas e dois-pontos.
2.  **Expansão Bibliográfica e Construção de Grafo (`build_citation_graph`)**: Constrói um grafo unificado direcionado (`networkx.DiGraph`) ligando múltiplos DOIs sementes (*seeds*) a seus respectivos trabalhos referenciados (expansão *backward*) e trabalhos citadores (expansão *forward*).

---

## 2. Decisões de Design e Otimizações de Rede

### A. O Bug Crítico de Referências (Nós *Backward*) Resolvido
*   **O Bug:** Na API do OpenAlex, a lista de referências de um artigo (`referenced_works`) retorna URLs contendo IDs canônicos no formato `https://openalex.org/W19621276`.
*   **O Impacto:** O script original realizava a requisição `requests.get` diretamente neste link. No entanto, o domínio `openalex.org` aponta para o site público HTML. Ao tentar ler esse HTML como JSON (`r.json()`), o script quebrava com `JSONDecodeError` e descartava a referência de forma silenciosa.
*   **A Correção:** O método `get_referenced_works` intercepta o ID canônico e converte o domínio público para o endpoint de API do OpenAlex (`api.openalex.org/works/`), garantindo que o retorno seja JSON autêntico.
*   **Tolerância a Falhas:** Cada chamada de referência individual está envelopada em um bloco `try...except`, garantindo que se uma referência específica falhar (ex: HTTP 404), as demais continuem a ser processadas.

### B. Cache LRU com Tamanho Máximo Controlado
*   **Problema de Redundância:** Em grafos de múltiplos seeds, artigos de uma mesma área científica frequentemente compartilham as mesmas referências (*co-citation*). Sem cache, o agente faria chamadas HTTP redundantes para a API.
*   **A Implementação:** A classe gerencia um `collections.OrderedDict` (`self._works_cache`) com política LRU via `_cache_set()`. O tamanho máximo é controlado por `self._cache_maxsize = 1000`. Ao atingir o limite, a entrada mais antiga é descartada automaticamente (`popitem(last=False)`).
*   **Indexação Dupla:** Cada trabalho é registrado tanto pelo DOI quanto pelo ID canônico do OpenAlex, pois o mesmo paper pode ser referenciado de duas formas distintas durante a construção do grafo.

### C. Autenticação por Header HTTP Bearer (Segurança)
*   **Problema Anterior:** A chave de API era enviada como query parameter na URL (`?api_key=...`), ficando exposta em logs de servidor, logs de proxy e mensagens de erro.
*   **A Correção:** No `__init__`, a chave é adicionada ao header HTTP da sessão via `self.headers["Authorization"] = f"Bearer {self.api_key}"`. Todos os métodos que faziam `params["api_key"] = self.api_key` foram refatorados.

### D. Higienização Automática de Filtros de Busca
*   **Problema de Sintaxe:** O OpenAlex utiliza a vírgula (`,`) como delimitador de filtros e o dois-pontos (`:`) para associar chave e valor. Queries contendo esses caracteres causavam `400 Bad Request`.
*   **A Implementação:** O método `search_by_keyword` substitui automaticamente `,` e `:` por espaços antes de montar o filtro.

---

## 3. Rastreabilidade de Dados e Proveniência (Auditoria do Pipeline)

*   **Identificação de Falhas:** O método `build_citation_graph` rastreia falhas em `G.graph["failed_seeds"]`. Sementes que falham não interrompem o processamento das demais.
*   **Origem dos Dados (Fix de Encapsulamento):** O metadado `G.graph["source"]` é definido via parâmetro `source: str = "api"` diretamente na chamada de `build_citation_graph`. O chamador não deve sobrescrever o metadado externamente após o retorno do método.

---

## 4. Análise de Centralidade e Conectividade do Grafo

**PageRank sobre Grafos Desconectados:** O script emite aviso automático quando `nx.number_weakly_connected_components(G) > 1`. Para análises rigorosas, itere sobre cada componente com `nx.weakly_connected_components(G)` e calcule PageRank por subgrafo separadamente.

**Grafo Vazio — Falha Explícita:** Se todas as sementes falharem, `rank_by_centrality()` lança `ValueError` com mensagem diagnóstica explícita em vez de retornar uma lista vazia silenciosa. Verifique `G.graph["failed_seeds"]` para diagnóstico.

---

## 5. Como Integrar em Outro Módulo

```python
from discovery_agent import SearchAgent

agent = SearchAgent(contact_email="seu@email.com", api_key="SUA_CHAVE")

papers = agent.search_by_keyword("resistance training hypertrophy", limit=10)

G = agent.build_citation_graph(
    seed_dois=["10.1519/jsc.0b013e3181e840f3", "10.1007/s40279-014-0264-9"],
    backward_limit=15,
    forward_limit=15,
    source="api"
)

try:
    ranking = agent.rank_by_centrality(G)
except ValueError as e:
    print(f"Diagnóstico: {e}")

agent.export_interactive_html(G, out_path="meu_grafo.html")
```
