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
from config import SEARCH_PROVIDER
from memory.semantic_cache import SemanticCache
from models import ResearchOutput, TaskJob, TaskResult, TaskStatus
from tools.native_search import NativeSearchNoDisponible, NativeSearchTool
from tools.web_scraper import WebScraperTool
from tools.web_search import MAX_QUERY_CHARS, SearchError, WebSearchTool
from utils.llm_factory import get_llm_client

logger = logging.getLogger("researcher")

# Cuántas de las fuentes encontradas por Tavily vale la pena
# leer completas con Firecrawl. Subir este número da más contexto
# pero sube costo y latencia linealmente.
TOP_SOURCES_TO_SCRAPE = 3

# Tope de contenido por fuente al armar el prompt de síntesis. Un
# directorio de proveedores scrapeado completo puede traer >50.000
# caracteres; sin este recorte el modelo agota su presupuesto de salida
# y devuelve un JSON cortado a la mitad.
MAX_CHARS_PER_SOURCE = 6000

# Presupuesto de salida para la síntesis. 3000 era insuficiente para un
# resumen de 3-5 párrafos más hallazgos y fuentes: el JSON se truncaba.
SYNTHESIS_MAX_TOKENS = 8000

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

QUERY_SYSTEM_PROMPT = """Convierte la instrucción del usuario en UNA consulta \
de búsqueda web, como la escribirías en Google.

Reglas:
- Entre 4 y 15 palabras. Nunca una sola palabra: una consulta genérica \
devuelve artículos de definición inútiles.
- CONSERVA SIEMPRE, si aparecen en la instrucción: el sector o tema, el \
lugar (ciudad, país), el año, y los nombres propios de empresas, \
productos o personas. Son lo que hace la búsqueda específica.
- QUITA: verbos de instrucción ("investiga", "lista", "identifica"), \
el formato de salida pedido (CSV, tabla, informe) y la enumeración de \
campos a recopilar (teléfono, dirección, correo).
- Mantén el idioma del original.

Ejemplo
Instrucción: "Investiga y lista los proveedores de tecnología más grandes \
en Bogotá, Colombia, que operan como distribuidores mayoristas similar a \
Anixter y Wesco. Para cada uno recopila nombre, sitio web, dirección, \
teléfono y categorías de producto. La salida debe ser una tabla \
convertible a CSV."
Consulta: distribuidores mayoristas de tecnología en Bogotá Colombia

Responde ÚNICAMENTE con la consulta, en una sola línea, sin comillas, \
sin explicación y sin prefijos."""

# Una consulta condensada por debajo de esto casi siempre significa que el
# LLM se pasó de agresivo y perdió el lugar o el sector (se observó
# "Proveedores" a secas a partir de un prompt de 713 caracteres). En ese
# caso es preferible el recorte mecánico, que conserva el inicio del
# prompt con sus entidades.
MIN_QUERY_WORDS = 3


class ResearcherAgent(BaseAgent):
    name = "researcher"

    def __init__(self):
        self.llm = get_llm_client()

        # Búsqueda nativa del proveedor de LLM: una sola clave y una sola
        # cuota, en vez de LLM + Tavily + Firecrawl. Si el proveedor no la
        # soporta o SEARCH_PROVIDER pide Tavily, se usa el camino clásico.
        self.native = None
        if SEARCH_PROVIDER in ("auto", "native"):
            try:
                self.native = NativeSearchTool()
                logger.info(f"Investigador usando búsqueda nativa de {self.native.proveedor}")
            except NativeSearchNoDisponible as exc:
                if SEARCH_PROVIDER == "native":
                    raise
                logger.info(f"Búsqueda nativa no disponible ({exc}); se usará Tavily + Firecrawl")

        # Las herramientas clásicas solo se construyen si se van a usar:
        # exigen TAVILY_API_KEY y FIRECRAWL_API_KEY en el constructor, y no
        # tiene sentido pedirlas cuando la búsqueda nativa las reemplaza.
        self.search_tool = None
        self.scraper_tool = None
        if self.native is None:
            self.search_tool = WebSearchTool()
            self.scraper_tool = WebScraperTool()

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

        if self.native is not None:
            return self._investigar_nativo(job, prompt)

        # El prompt de una subtarea del Director es una instrucción
        # descriptiva (a veces >400 caracteres), no una consulta de
        # búsqueda. Mandarlo tal cual a Tavily devolvía 400 Bad Request.
        query = self._build_search_query(prompt)

        try:
            search_results = self.search_tool.search(query, max_results=8)
        except SearchError as exc:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"La búsqueda web falló: {exc}",
            )

        if not search_results:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"La búsqueda web no encontró resultados para '{query}'.",
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

    def _investigar_nativo(self, job: TaskJob, prompt: str) -> TaskResult:
        """
        Camino de una sola llamada: el modelo busca, lee y sintetiza.

        No necesita condensar la consulta (no hay límite de 400 caracteres
        como en Tavily) ni scrapear aparte: la instrucción descriptiva de
        la subtarea se pasa tal cual.
        """
        try:
            datos = self.native.buscar_y_sintetizar(
                objetivo=f"TEMA A INVESTIGAR:\n{prompt}",
                system_prompt=SYSTEM_PROMPT,
                max_tokens=SYNTHESIS_MAX_TOKENS,
            )
            salida = ResearchOutput.model_validate(datos)
        except Exception as exc:
            logger.exception(f"La búsqueda nativa falló para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"La búsqueda web nativa falló: {exc}",
            )

        if not salida.summary.strip():
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error="La búsqueda nativa no devolvió contenido utilizable.",
            )

        resultado = salida.model_dump()
        self._store_cache(prompt, resultado)
        logger.info(f"Búsqueda nativa completada con {len(salida.sources)} fuente(s)")

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result={**resultado, "fromCache": False, "searchMode": "native"},
        )

    def _build_search_query(self, prompt: str) -> str:
        """
        Convierte la instrucción de la subtarea en una consulta de búsqueda
        corta y con palabras clave.

        Si el prompt ya cabe en el límite de Tavily, se usa tal cual: no
        vale gastar una llamada al LLM. Si se pasa, se le pide al modelo
        que lo condense; si esa llamada falla, se recorta mecánicamente
        (WebSearchTool.shorten_query) en vez de tumbar la investigación.
        """
        if len(prompt) <= MAX_QUERY_CHARS:
            return prompt

        try:
            consulta = self.llm.complete(
                system=QUERY_SYSTEM_PROMPT,
                user=f"INSTRUCCIÓN:\n{prompt}",
                max_tokens=100,
            ).strip().strip('"').splitlines()[0]

            if not consulta or len(consulta) > MAX_QUERY_CHARS:
                logger.warning(f"Consulta condensada inválida ({len(consulta)} caracteres), se recorta el prompt")
            elif len(consulta.split()) < MIN_QUERY_WORDS:
                # Sobre-compresión: perdió lugar/sector/nombres propios.
                logger.warning(
                    f"Consulta condensada demasiado genérica ('{consulta}'), "
                    "se recorta el prompt original para conservar contexto"
                )
            else:
                logger.info(f"Consulta condensada por el LLM: '{consulta}'")
                return consulta
        except Exception:
            logger.exception("No se pudo condensar la consulta con el LLM, se recorta mecánicamente")

        return WebSearchTool.shorten_query(prompt)

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
        scrapeadas = 0
        for source in search_results:
            # Tavily devuelve a veces rutas relativas de redirección
            # ("/goto?url=..."), no URLs absolutas. Firecrawl las rechaza
            # con 400 y además no sirven como cita verificable para el
            # usuario, así que ni se intentan scrapear.
            url = source.get("url", "")
            if not url.startswith(("http://", "https://")):
                logger.info(f"Fuente descartada por URL no absoluta: {url[:60]}")
                continue

            if scrapeadas < TOP_SOURCES_TO_SCRAPE:
                full_content = self.scraper_tool.scrape(url)
                scrapeadas += 1
                if full_content:
                    enriched.append({**source, "content": full_content, "scraped": True})
                    continue
            enriched.append({**source, "scraped": False})
        return enriched

    def _synthesize(self, prompt: str, sources: list[dict]) -> ResearchOutput:
        # Recortar cada fuente: un directorio o catálogo scrapeado puede
        # traer decenas de miles de caracteres, y meterlos todos empuja al
        # modelo contra su límite de salida — que es como se truncaba el
        # JSON a media cadena y fallaba el json.loads.
        sources_block = "\n\n---\n\n".join(
            f"FUENTE: {s['title']}\nURL: {s['url']}\nCONTENIDO:\n{(s.get('content') or '')[:MAX_CHARS_PER_SOURCE]}"
            for s in sources
        )

        user_message = f"TEMA A INVESTIGAR: {prompt}\n\nFUENTES ENCONTRADAS:\n\n{sources_block}"

        raw_response = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=user_message,
            max_tokens=SYNTHESIS_MAX_TOKENS,
            json_mode=True,
        )

        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            # Por si el modelo ignora la instrucción de no usar backticks
            cleaned = cleaned.strip("`").removeprefix("json").strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Distinguir "el modelo devolvió basura" de "la respuesta se
            # cortó por límite de tokens" ahorra mucho tiempo de
            # diagnóstico: el segundo caso se arregla subiendo el límite
            # o recortando más las fuentes, no tocando el prompt.
            if "Unterminated" in str(exc) or "Expecting" in str(exc):
                raise ValueError(
                    f"La respuesta del LLM llegó incompleta ({len(cleaned)} caracteres, "
                    f"límite {SYNTHESIS_MAX_TOKENS} tokens): {exc}"
                ) from exc
            raise

        return ResearchOutput.model_validate(parsed)
