"""
Wrapper de la API de Firecrawl (scraping de páginas a Markdown limpio).
Docs: https://docs.firecrawl.dev

Requiere FIRECRAWL_API_KEY en el entorno.

Tavily ya da un resumen de cada página en sus resultados de búsqueda,
así que Firecrawl se reserva para cuando ese resumen no basta: el
Investigador decide qué URLs vale la pena "leer completas".
"""
import logging
import os

from firecrawl import FirecrawlApp

logger = logging.getLogger("tools.web_scraper")

# Límite de caracteres por página scrapeada, para no inflar el contexto
# del LLM con páginas enormes. 6000 chars ~ 1500 tokens, suficiente para
# síntesis sin disparar costos.
MAX_CONTENT_CHARS = 6000


class WebScraperTool:
    def __init__(self, api_key: str | None = None):
        key = api_key or os.getenv("FIRECRAWL_API_KEY")
        if not key:
            raise RuntimeError(
                "FIRECRAWL_API_KEY no está configurada. "
                "Define la variable de entorno antes de iniciar el worker."
            )
        self.client = FirecrawlApp(api_key=key)

    def scrape(self, url: str) -> str | None:
        """
        Devuelve el contenido en Markdown de una URL, truncado a
        MAX_CONTENT_CHARS. Devuelve None si el scrape falla — el
        agente que llama debe manejar ese caso sin romper el flujo
        (una fuente fallida no debe tumbar toda la investigación).
        """
        logger.info(f"Firecrawl scrape: {url}")

        try:
            result = self.client.scrape_url(url, params={"formats": ["markdown"]})
        except Exception:
            logger.exception(f"Firecrawl falló scrapeando {url}")
            return None

        markdown = result.get("markdown") if isinstance(result, dict) else None
        if not markdown:
            logger.warning(f"Firecrawl no devolvió markdown para {url}")
            return None

        return markdown[:MAX_CONTENT_CHARS]
