"""
Wrapper de la API de Tavily (búsqueda web optimizada para agentes/LLMs).
Docs: https://docs.tavily.com

Requiere TAVILY_API_KEY en el entorno. Nunca hardcodear la key aquí —
viene siempre de una variable de entorno inyectada por Docker/`.env`.
"""
import logging
import os
from typing import TypedDict

from tavily import TavilyClient

logger = logging.getLogger("tools.web_search")


class SearchResult(TypedDict):
    title: str
    url: str
    content: str
    score: float


class SearchError(RuntimeError):
    """
    La búsqueda no se pudo ejecutar (error de API, red o consulta inválida).

    Se distingue del caso "la búsqueda corrió pero no encontró nada", que
    devuelve una lista vacía. Antes ambos casos devolvían [], y el
    Investigador reportaba siempre "verifica TAVILY_API_KEY o la
    conectividad" — un mensaje engañoso cuando la clave estaba bien y el
    problema real era, por ejemplo, una consulta demasiado larga.
    """


# Límite duro de la API de Tavily. Una consulta más larga devuelve
# 400 Bad Request. El Director genera prompts descriptivos que superan
# este límite con facilidad, así que se recorta antes de enviar.
MAX_QUERY_CHARS = 380


class WebSearchTool:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.getenv("TAVILY_API_KEY")
        if not key:
            raise RuntimeError(
                "TAVILY_API_KEY no está configurada. "
                "Define la variable de entorno antes de iniciar el worker."
            )
        self.client = TavilyClient(api_key=key)

    @staticmethod
    def shorten_query(query: str, limit: int = MAX_QUERY_CHARS) -> str:
        """
        Recorta la consulta al límite de Tavily, cortando en el último
        espacio para no partir una palabra por la mitad.
        """
        query = " ".join(query.split())
        if len(query) <= limit:
            return query
        recortada = query[:limit]
        corte = recortada.rfind(" ")
        if corte > limit * 0.6:
            recortada = recortada[:corte]
        logger.warning(
            f"Consulta de {len(query)} caracteres recortada a {len(recortada)} "
            f"(límite de Tavily: {limit})"
        )
        return recortada.rstrip(" ,.;:")

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Búsqueda general. `search_depth='advanced'` cuesta más créditos
        pero da mejor contenido para síntesis con LLM que 'basic'.

        Lanza SearchError si la búsqueda no se pudo ejecutar. Devuelve
        lista vacía solo si corrió bien y no hubo resultados.
        """
        query = self.shorten_query(query)
        logger.info(f"Tavily search: '{query}' (max_results={max_results})")

        try:
            response = self.client.search(
                query=query,
                search_depth="advanced",
                max_results=max_results,
                include_answer=False,
            )
        except Exception as exc:
            logger.exception(f"Tavily search falló para query='{query}'")
            raise SearchError(f"{type(exc).__name__}: {exc}") from exc

        results: list[SearchResult] = []
        for item in response.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    content=item.get("content", ""),
                    score=item.get("score", 0.0),
                )
            )

        logger.info(f"Tavily devolvió {len(results)} resultados para '{query}'")
        return results
