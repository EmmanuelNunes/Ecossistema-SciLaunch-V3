import requests
import networkx as nx
from pyvis.network import Network
from time import sleep
from collections import OrderedDict

OPENALEX_BASE = "https://api.openalex.org/works"

# Boa prática de API pública: identificar-se no header/param para entrar
# no "polite pool" da OpenAlex (rate limit mais generoso, menos throttle)
HEADERS = {"User-Agent": "SciLaunch-DiscoveryAgent (mailto:emmanuel.nunes.discovery@gmail.com)"}


class SearchAgent:
    """
    Substitui o SearchAgent perdido. Duas capacidades, deixadas
    explícitas como métodos separados para não repetir o erro de
    empacotar algo vago sob um nome bonito.
    """

    def __init__(self, contact_email: str = "emmanuel.nunes.discovery@gmail.com", api_key: str | None = None):
        import os
        # OpenAlex pede identificação para priorizar no "polite pool"
        self.headers = {"User-Agent": f"SciLaunch-SearchAgent (mailto:{contact_email})"}
        # Se não for passada api_key, tenta carregar da variável de ambiente OPENALEX_API_KEY
        self.api_key = api_key or os.environ.get("OPENALEX_API_KEY")
        # Fix 1: autenticação via header HTTP Bearer — não expõe a chave em URLs, logs de proxy ou histórico de rede
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"
        # Fix 3: cache LRU com tamanho máximo para evitar crescimento ilimitado em sessões longas
        self._works_cache: OrderedDict = OrderedDict()
        self._cache_maxsize = 1000

    def _cache_set(self, key: str, value: dict) -> None:
        """Registra um item no cache LRU. Descarta o mais antigo ao atingir maxsize."""
        if key in self._works_cache:
            self._works_cache.move_to_end(key)
        self._works_cache[key] = value
        if len(self._works_cache) > self._cache_maxsize:
            self._works_cache.popitem(last=False)  # Remove a entrada mais antiga (política FIFO/LRU)

    # ------------------------------------------------------------------
    # 1. BUSCA POR TEXTO — o que faltava confirmadamente no sistema antigo
    # ------------------------------------------------------------------
    def search_by_keyword(self, query: str, limit: int = 20, filters: dict | None = None, title_and_abstract_only: bool = True) -> list[dict]:
        """
        Busca real por palavra-chave na OpenAlex, não retrieval sobre
        Zotero pré-carregado. Isso é o que faz o sistema achar papers
        que você nunca importou manualmente.

        filters: dict opcional, ex: {"from_publication_date": "2015-01-01"}
        title_and_abstract_only: se True, restringe a busca ao título e resumo para aumentar a precisão de domínio.
        """
        # Higieniza a query removendo caracteres que quebram o interpretador de filtros do OpenAlex
        clean_query = query.replace(",", " ").replace(":", " ")
        clean_query = " ".join(clean_query.split())

        params = {"per-page": limit}
        
        filter_list = []
        if title_and_abstract_only:
            # A busca em campos específicos no OpenAlex deve ser enviada como um filtro
            filter_list.append(f"title_and_abstract.search:{clean_query}")
        else:
            params["search"] = clean_query
            
        if filters:
            for k, v in filters.items():
                filter_list.append(f"{k}:{v}")
                
        if filter_list:
            params["filter"] = ",".join(filter_list)
            
        r = requests.get(OPENALEX_BASE, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    # ------------------------------------------------------------------
    # 2. EXPANSÃO POR CITAÇÃO — o que a v1 deste arquivo já fazia
    # ------------------------------------------------------------------
    def get_work_by_doi(self, doi: str) -> dict:
        doi_key = doi.lower().strip()
        if doi_key in self._works_cache:
            return self._works_cache[doi_key]

        url = f"{OPENALEX_BASE}/https://doi.org/{doi}"
        r = requests.get(url, headers=self.headers, timeout=15)
        r.raise_for_status()
        work = r.json()
        
        # Alimenta o cache com o DOI e com o ID do OpenAlex
        self._cache_set(doi_key, work)
        work_id = work.get("id")
        if work_id:
            self._cache_set(work_id.lower(), work)
            
        return work

    def get_referenced_works(self, work: dict, limit: int = 15) -> list[dict]:
        """BACKWARD: papers que o work cita."""
        ref_ids = work.get("referenced_works", [])[:limit]
        results = []
        for wid in ref_ids:
            wid_key = wid.lower()
            if wid_key in self._works_cache:
                results.append(self._works_cache[wid_key])
                continue

            # Corrige o ID canônico para apontar para a API ao invés do site público HTML
            api_url = wid.replace("https://openalex.org/", f"{OPENALEX_BASE}/")
            try:
                r = requests.get(api_url, headers=self.headers, timeout=15)
                if r.ok:
                    res_json = r.json()
                    results.append(res_json)
                    # Registra no cache
                    self._cache_set(wid_key, res_json)
                    doi = res_json.get("doi")
                    if doi:
                        clean_doi = doi.replace("https://doi.org/", "").lower().strip()
                        self._cache_set(clean_doi, res_json)
            except Exception as e:
                print(f"[Erro] Falha ao recuperar referência {wid}: {e}")
            sleep(0.1)  # respeito ao rate limit, não é decoração
        return results

    def get_citing_works(self, work_id: str, limit: int = 15) -> list[dict]:
        """FORWARD: quem cita o work. Filtro cites: na API de listagem."""
        params = {"filter": f"cites:{work_id}", "per-page": limit}
        r = requests.get(OPENALEX_BASE, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        works = r.json().get("results", [])
        
        # Alimenta o cache com os trabalhos citadores encontrados
        for work in works:
            w_id = work.get("id")
            if w_id:
                self._cache_set(w_id.lower(), work)
            doi = work.get("doi")
            if doi:
                clean_doi = doi.replace("https://doi.org/", "").lower().strip()
                self._cache_set(clean_doi, work)
                
        return works

    def build_citation_graph(self, seed_dois: str | list[str], backward_limit=15, forward_limit=15, source: str = "api") -> nx.DiGraph:
        """
        Monta o grafo dirigido: aresta A -> B significa 'A cita B'.
        Aceita um DOI único ou uma lista de DOIs sementes (3-5) para produzir
        um grafo com interconexões ricas e PageRank estatisticamente útil.

        source: identificador de proveniência dos dados ('api', 'fallback_static', etc.).
                Fix 2: deve ser definido pelo chamador aqui — não sobrescrever externamente após o retorno.
        """
        G = nx.DiGraph()
        
        # Normaliza a entrada para ser sempre uma lista
        if isinstance(seed_dois, str):
            seed_dois = [seed_dois]
            
        G.graph["source"] = source
        G.graph["seeds"] = seed_dois
        G.graph["failed_seeds"] = []
        
        for seed_doi in seed_dois:
            try:
                seed = self.get_work_by_doi(seed_doi)
                seed_id = seed["id"]
                # Adiciona ou atualiza o nó semente no grafo
                G.add_node(seed_id, title=seed.get("display_name", "seed"), kind="seed")

                for ref in self.get_referenced_works(seed, limit=backward_limit):
                    rid = ref["id"]
                    # Evita rebaixar um nó que é semente para "backward"
                    if rid not in G or G.nodes[rid].get("kind") != "seed":
                        G.add_node(rid, title=ref.get("display_name", "?"), kind="backward")
                    G.add_edge(seed_id, rid)

                for citer in self.get_citing_works(seed_id, limit=forward_limit):
                    cid = citer["id"]
                    # Evita rebaixar um nó que é semente para "forward"
                    if cid not in G or G.nodes[cid].get("kind") != "seed":
                        G.add_node(cid, title=citer.get("display_name", "?"), kind="forward")
                    G.add_edge(cid, seed_id)
            except Exception as e:
                print(f"[Erro] Falha ao processar a semente {seed_doi}: {e}")
                G.graph["failed_seeds"].append(seed_doi)

        return G

    def rank_by_centrality(self, G: nx.DiGraph) -> list[tuple[str, float]]:
        # Fix 4: falha explícita em vez de output silencioso quando o grafo está vazio
        if G.number_of_nodes() == 0:
            raise ValueError(
                "O grafo está vazio. Todas as sementes falharam no processamento. "
                "Verifique G.graph['failed_seeds'] para diagnóstico."
            )
        scores = nx.pagerank(G)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(G.nodes[nid].get("title", nid), score) for nid, score in ranked]

    def export_interactive_html(self, G: nx.DiGraph, out_path: str = "citation_graph.html"):
        net = Network(height="750px", width="100%", directed=True, notebook=False)
        color_map = {"seed": "#e63946", "backward": "#457b9d", "forward": "#2a9d8f"}
        for node, attrs in G.nodes(data=True):
            net.add_node(node, label=attrs.get("title", "")[:40], color=color_map.get(attrs.get("kind"), "#999999"))
        for src, dst in G.edges():
            net.add_edge(src, dst)
        net.write_html(out_path)
        print(f"Grafo salvo em {out_path}")


if __name__ == "__main__":
    import os
    api_key = os.environ.get("OPENALEX_API_KEY")
    if not api_key:
        print("\n" + "*" * 80)
        print("AVISO: Variável de ambiente 'OPENALEX_API_KEY' não detectada.")
        print("O script tentará executar no modo ANÔNIMO (sujeito a bloqueios 503 sob alta carga).")
        print("Para configurar a chave no PowerShell, execute:")
        print("  $env:OPENALEX_API_KEY=\"SUA_CHAVE_AQUI\"")
        print("*" * 80 + "\n")
    else:
        # Exibe apenas os primeiros e últimos caracteres por motivos de segurança
        masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "configurada"
        print(f"\n[Info] Utilizando chave de API detectada via variável de ambiente ({masked_key})\n")

    agent = SearchAgent(contact_email="emmanuel.nunes.discovery@gmail.com")

    # --- Teste de Higienização de Query ---
    DIRTY_QUERY = "resistance training, hypertrophy: structural variables"
    print(f"=== TESTE DE HIGIENIZAÇÃO DE QUERY ===")
    print(f"Query original: '{DIRTY_QUERY}'")
    try:
        # Testa a busca com a query suja contendo vírgulas e dois-pontos para garantir que ela não quebre com 400 Bad Request
        results_dirty = agent.search_by_keyword(DIRTY_QUERY, limit=3, title_and_abstract_only=True)
        print(f"Busca com query higienizada retornou {len(results_dirty)} resultados com sucesso (sem erro 400).")
    except Exception as e:
        print(f"[Aviso/Erro] Teste de higienização falhou (pode ser rate limit): {e}")
    print("=" * 40 + "\n")

    # --- Teste 1: busca por texto real refinada (Precisão de Domínio) ---
    QUERY = "resistance training hypertrophy"
    results = []
    try:
        results = agent.search_by_keyword(QUERY, limit=5, title_and_abstract_only=True)
        print(f"Busca Refinada por '{QUERY}': {len(results)} resultados")
        for w in results[:5]:
            print(f"  - {w.get('display_name')} ({w.get('publication_year')})")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 503:
            print("\n" + "!" * 80)
            print("ATENÇÃO: A BUSCA POR TEXTO (TESTE 1) FALHOU!")
            print("Erro HTTP 503 (Service Unavailable): A busca pública anônima do OpenAlex está rate-limited")
            print("devido a alta carga nos servidores deles. Para resolver isso em produção, use uma chave")
            print("de API gratuita obtida em https://openalex.org/rest-api.")
            print("!" * 80 + "\n")
        else:
            print(f"\n[Erro] Falha ao realizar busca por palavra-chave: {e}\n")

    # --- Teste 2: expansão por citação a partir de múltiplos DOIs reais ---
    seed_dois = []
    for w in results:
        doi_url = w.get("doi")
        if doi_url:
            seed_dois.append(doi_url.replace("https://doi.org/", ""))

    is_fallback = False
    if len(seed_dois) < 3:
        is_fallback = True
        seed_dois = [
            "10.1519/jsc.0b013e3181e840f3", # Mechanisms of Muscle Hypertrophy (2010)
            "10.1519/jsc.0000000000002200", # Low- vs. High-Load Resistance Training (2017)
            "10.1007/s40279-014-0264-9"      # Blood-Flow Restriction Training (2014)
        ]
        print("\n" + "=" * 80)
        print("AVISO DE SEGURANÇA DE DADOS (FALLBACK ATIVO):")
        print("Como a busca por texto falhou ou não retornou dados de sementes suficientes,")
        print("o script está utilizando 3 DOIs de fallback clássicos de hipertrofia.")
        print("Qualquer processamento subsequente deve marcar a origem destes dados.")
        print("=" * 80 + "\n")

    # Adicionamos propositalmente um DOI inválido para testar o rastreamento estruturado de falhas de seeds
    print("=== TESTE DE RASTREAMENTO DE SEEDS INVÁLIDOS ===")
    test_seeds = seed_dois.copy()
    test_seeds.append("10.9999/invalid-doi-test")
    print(f"Testando a construção de grafo com seeds: {test_seeds}")
    
    # Limitamos para 5 de cada lado para rodar o teste de forma ágil e evitar throttles
    # Fix 2: source declarado como parâmetro na chamada — não sobrescrito externamente após o retorno
    graph = agent.build_citation_graph(
        test_seeds,
        backward_limit=5,
        forward_limit=5,
        source="fallback_static" if is_fallback else "api"
    )
        
    print(f"\n=== RESULTADOS E AUDITORIA DO GRAFO ===")
    print(f"Proveniência dos dados do grafo (graph.graph['source']): {graph.graph.get('source')}")
    print(f"Sementes fornecidas: {graph.graph.get('seeds')}")
    print(f"Sementes que FALHARAM no processamento (failed_seeds): {graph.graph.get('failed_seeds')}")
    print(f"Grafo de Citações: {graph.number_of_nodes()} nós, {graph.number_of_edges()} arestas")

    # Verifica o número de componentes fracamente conectados para alertar sobre dispersão
    num_components = nx.number_weakly_connected_components(graph)
    print(f"Número de componentes desconectados (Weakly Connected Components): {num_components}")
    if num_components > 1:
        print("[Aviso Metodológico] O grafo possui múltiplos componentes desconectados.")
        print(" O PageRank global terá pesos distribuídos de forma isolada entre sementes.")
        
    # Mostra a economia de rede proporcionada pelo cache local
    print(f"Total de trabalhos em cache de memória local: {len(agent._works_cache)} itens.")

    print("\nRanking de Centralidade (PageRank) - Múltiplos Seeds:")
    for title, score in agent.rank_by_centrality(graph)[:10]:
        print(f"  {score:.4f}  {title}")

    agent.export_interactive_html(graph)



