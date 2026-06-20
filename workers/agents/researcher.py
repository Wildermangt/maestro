"""
Agente Investigador.

Flujo:
1. Revisa el caché semántico (Qdrant) — si hay un hit, devuelve eso.
2. Busca con Tavily (varios resultados, contenido resumido).
3. Scrapea con Firecrawl las top-N fuentes más relevantes para
   obtener el contenido completo (Tavily da solo un resumen corto).
4. Pasa todo a Claude para sintetizar un resumen + hallazgos clave,
   forzando que cite las fuentes reales que efectivamente usó.
5. Valida la salida del LLM contra ResearchOutput (Pydantic) —
   si el LLM no devuelve JSON válido, falla explícitamente en vez
   de inventar una estructura.
6. Guarda el resultado en caché semántico para la próxima vez.

Cada paso está aislado en try/except de forma que una fuente que
falla scrapeando, o el caché que no responde, no tumben toda la
investigación — se degrada con lo que sí se pudo obtener.
"""
import json
import logging

from agents.base import BaseAgent
from memory.semantic_cache import SemanticCache
from models import ResearchOutput, TaskJob, TaskResult, TaskStatus
from tools.web_scraper import WebScraperTool
from tools.web_search import WebSearchTool
from utils.llm_factory import get_llm_client

logger = logging.getLogger("researcher")

# Cuántas de las fuentes encontradas por Tavily vale la pena
# leer completas con Firecrawl. Subir este número da más contexto
# pero sube costo y latencia linealmente.
TOP_SOURCES_TO_SCRAPE = 3

SYSTEM_PROMPT = """Eres un Investigador experto con capacidad de síntesis y \
verificación de fuentes. Recibes resultados de búsqueda web (algunos con \
contenido completo, otros solo con un resumen corto) sobre un tema. Tu trabajo:

1. Sintetiza un resumen ejecutivo claro y directo (3-5 párrafos).
2. Extrae entre 3 y 7 hallazgos clave concretos (datos, cifras, hechos verificables).
3. Lista SOLO las fuentes que realmente usaste para construir el resumen \
y los hallazgos — no inventes fuentes ni incluyas las que no aportaron nada.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto, sin texto \
adicional antes o después, sin backticks de markdown:

{
  "summary": "string",
  "key_findings": ["string", "string", ...],
  "sources": [{"title": "string", "url": "string"}, ...]
}"""


class ResearcherAgent(BaseAgent):
    name = "researcher"

    def __init__(self):
        self.search_tool = WebSearchTool()
        self.scraper_tool = WebScraperTool()
        self.llm = get_llm_client(provider="claude")
        try:
            self.cache = SemanticCache()
        except Exception:
            logger.exception("No se pudo inicializar el caché semántico (Qdrant). Continuando sin caché.")
            self.cache = None

    def run(self, job: TaskJob) -> TaskResult:
        prompt = job.prompt
        logger.info(f"ResearcherAgent procesando taskId={job.taskId} prompt='{prompt}'")

        cached = self._check_cache(prompt)
        if cached is not None:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.COMPLETED,
                result={**cached, "fromCache": True},
            )

        search_results = self.search_tool.search(prompt, max_results=8)

        if not search_results:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error="La búsqueda web no devolvió resultados. Verifica TAVILY_API_KEY o la conectividad.",
            )

        enriched_sources = self._scrape_top_sources(search_results)

        try:
            research_output = self._synthesize(prompt, enriched_sources)
        except Exception as exc:
            logger.exception(f"Falló la síntesis con LLM para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.PARTIAL,
                result={
                    "summary": None,
                    "rawSearchResults": search_results,
                },
                error=f"La síntesis con el LLM falló: {exc}. Se devuelven los resultados crudos de búsqueda.",
            )

        result_dict = research_output.model_dump()
        self._store_cache(prompt, result_dict)

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result={**result_dict, "fromCache": False},
        )

    def _check_cache(self, prompt: str) -> dict | None:
        if self.cache is None:
            return None
        try:
            return self.cache.find_similar(prompt)
        except Exception:
            logger.exception("Error consultando el caché semántico, se ignora y se investiga de nuevo")
            return None

    def _store_cache(self, prompt: str, result: dict) -> None:
        if self.cache is None:
            return
        try:
            self.cache.store(prompt, result)
        except Exception:
            logger.exception("Error guardando en el caché semántico, se ignora")

    def _scrape_top_sources(self, search_results: list[dict]) -> list[dict]:
        """
        Enriquece las top-N fuentes con contenido completo de Firecrawl.
        Las demás se quedan con el resumen corto que ya dio Tavily.
        Una fuente que falla al scrapear simplemente conserva su resumen
        corto en vez de tumbar toda la investigación.
        """
        enriched = []
        for i, source in enumerate(search_results):
            if i < TOP_SOURCES_TO_SCRAPE:
                full_content = self.scraper_tool.scrape(source["url"])
                if full_content:
                    enriched.append({**source, "content": full_content, "scraped": True})
                    continue
            enriched.append({**source, "scraped": False})
        return enriched

    def _synthesize(self, prompt: str, sources: list[dict]) -> ResearchOutput:
        sources_block = "\n\n---\n\n".join(
            f"FUENTE: {s['title']}\nURL: {s['url']}\nCONTENIDO:\n{s['content']}"
            for s in sources
        )

        user_message = f"TEMA A INVESTIGAR: {prompt}\n\nFUENTES ENCONTRADAS:\n\n{sources_block}"

        raw_response = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=user_message,
            max_tokens=3000,
        )

        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            # Por si el modelo ignora la instrucción de no usar backticks
            cleaned = cleaned.strip("`").removeprefix("json").strip()

        parsed = json.loads(cleaned)
        return ResearchOutput.model_validate(parsed)
