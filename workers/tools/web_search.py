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


class WebSearchTool:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.getenv("TAVILY_API_KEY")
        if not key:
            raise RuntimeError(
                "TAVILY_API_KEY no está configurada. "
                "Define la variable de entorno antes de iniciar el worker."
            )
        self.client = TavilyClient(api_key=key)

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """
        Búsqueda general. `search_depth='advanced'` cuesta más créditos
        pero da mejor contenido para síntesis con LLM que 'basic'.
        """
        logger.info(f"Tavily search: '{query}' (max_results={max_results})")

        try:
            response = self.client.search(
                query=query,
                search_depth="advanced",
                max_results=max_results,
                include_answer=False,
            )
        except Exception:
            logger.exception(f"Tavily search falló para query='{query}'")
            return []

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
