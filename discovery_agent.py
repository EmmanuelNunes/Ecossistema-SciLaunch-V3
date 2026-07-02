import requests
import networkx as nx
from pyvis.network import Network
from time import sleep

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

    def __init__(self, contact_email: str = "emmanuel.nunes.discovery@gmail.com"):
        # OpenAlex pede identificação para priorizar no "polite pool"
        self.headers = {"User-Agent": f"SciLaunch-SearchAgent (mailto:{contact_email})"}

    # ------------------------------------------------------------------
    # 1. BUSCA POR TEXTO — o que faltava confirmadamente no sistema antigo
    # ------------------------------------------------------------------
    def search_by_keyword(self, query: str, limit: int = 20, filters: dict | None = None) -> list[dict]:
        """
        Busca real por palavra-chave na OpenAlex, não retrieval sobre
        Zotero pré-carregado. Isso é o que faz o sistema achar papers
        que você nunca importou manualmente.

        filters: dict opcional, ex: {"from_publication_date": "2015-01-01"}
        """
        params = {"search": query, "per-page": limit}
        if filters:
            filter_str = ",".join(f"{k}:{v}" for k, v in filters.items())
            params["filter"] = filter_str

        r = requests.get(OPENALEX_BASE, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    # ------------------------------------------------------------------
    # 2. EXPANSÃO POR CITAÇÃO — o que a v1 deste arquivo já fazia
    # ------------------------------------------------------------------
    def get_work_by_doi(self, doi: str) -> dict:
        url = f"{OPENALEX_BASE}/https://doi.org/{doi}"
        r = requests.get(url, headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def get_referenced_works(self, work: dict, limit: int = 15) -> list[dict]:
        """BACKWARD: papers que o work cita."""
        ref_ids = work.get("referenced_works", [])[:limit]
        results = []
        for wid in ref_ids:
            r = requests.get(wid, headers=self.headers, timeout=15)
            if r.ok:
                results.append(r.json())
            sleep(0.1)  # respeito ao rate limit, não é decoração
        return results

    def get_citing_works(self, work_id: str, limit: int = 15) -> list[dict]:
        """FORWARD: quem cita o work. Filtro cites: na API de listagem."""
        params = {"filter": f"cites:{work_id}", "per-page": limit}
        r = requests.get(OPENALEX_BASE, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    def build_citation_graph(self, seed_doi: str, backward_limit=15, forward_limit=15) -> nx.DiGraph:
        """
        Monta o grafo dirigido: aresta A -> B significa 'A cita B'.
        Aviso honesto: com um único seed e ~30 nós, PageRank sobre esse
        grafo é quase decorativo. Use múltiplos seeds (3-5) antes de
        confiar em qualquer ranking de centralidade.
        """
        G = nx.DiGraph()
        seed = self.get_work_by_doi(seed_doi)
        seed_id = seed["id"]
        G.add_node(seed_id, title=seed.get("display_name", "seed"), kind="seed")

        for ref in self.get_referenced_works(seed, limit=backward_limit):
            rid = ref["id"]
            G.add_node(rid, title=ref.get("display_name", "?"), kind="backward")
            G.add_edge(seed_id, rid)

        for citer in self.get_citing_works(seed_id, limit=forward_limit):
            cid = citer["id"]
            G.add_node(cid, title=citer.get("display_name", "?"), kind="forward")
            G.add_edge(cid, seed_id)

        return G

    def rank_by_centrality(self, G: nx.DiGraph) -> list[tuple[str, float]]:
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
    agent = SearchAgent(contact_email="emmanuel.nunes.discovery@gmail.com")

    # --- Teste 1: busca por texto real (o que faltava) ---
    QUERY = "resistance training structural variables"
    results = []
    try:
        results = agent.search_by_keyword(QUERY, limit=10)
        print(f"Busca por '{QUERY}': {len(results)} resultados")
        for w in results[:5]:
            print(f"  - {w.get('display_name')} ({w.get('publication_year')})")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 503:
            print(f"\n[Aviso] A busca por texto da API do OpenAlex está temporariamente indisponível (HTTP 503 - Rate Limit).")
            print("Isso ocorre devido a alta carga no motor de busca pública anônima do OpenAlex.")
            print("Prosseguindo com o Teste 2 usando o DOI de fallback...\n")
        else:
            print(f"\n[Erro] Falha ao realizar busca por palavra-chave: {e}\n")

    # --- Teste 2: expansão por citação a partir de um DOI real ---
    seed_doi = None
    for w in results:
        doi_url = w.get("doi")
        if doi_url:
            seed_doi = doi_url.replace("https://doi.org/", "")
            print(f"\nUsando o DOI encontrado na busca para o teste de expansão: {seed_doi}")
            break
            
    if not seed_doi:
        seed_doi = "10.1152/jappl.1989.66.1.232" # Fallback clássico
        print(f"\nUsando DOI clássico de fallback: {seed_doi}")

    # Limitamos para 5 de cada lado para rodar o teste de forma ágil e evitar throttles
    graph = agent.build_citation_graph(seed_doi, backward_limit=5, forward_limit=5)
    print(f"\nGrafo: {graph.number_of_nodes()} nós, {graph.number_of_edges()} arestas")

    print("\nTop 10 por centralidade (lembre: pouco confiável com 1 seed só):")
    for title, score in agent.rank_by_centrality(graph)[:10]:
        print(f"  {score:.4f}  {title}")

    agent.export_interactive_html(graph)


