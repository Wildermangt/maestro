"""
Búsqueda web nativa del proveedor de LLM.

Elimina la dependencia de Tavily y Firecrawl: en vez de buscar, scrapear y
luego pedirle al modelo que sintetice —tres servicios, tres claves, tres
cuotas—, el modelo busca y lee él mismo dentro de la misma llamada.

Soporte por proveedor:
  - claude : herramientas de servidor `web_search` / `web_fetch`.
  - gemini : grounding con Google Search.
  - openai : no implementado aquí; cae a Tavily.

La interfaz devuelve lo mismo que el flujo clásico (texto sintetizado +
lista de fuentes con URL), para que el Investigador no tenga que saber
cuál de los dos caminos se usó.
"""
import json
import logging
import os
import re

logger = logging.getLogger("tools.native_search")


class NativeSearchNoDisponible(RuntimeError):
    """El proveedor configurado no ofrece búsqueda web nativa."""


def proveedor_actual() -> str:
    return (os.getenv("LLM_PROVIDER") or "claude").strip().lower()


def soporta_busqueda_nativa(proveedor: str | None = None) -> bool:
    return (proveedor or proveedor_actual()) in ("claude", "gemini")


class NativeSearchTool:
    """
    Busca y sintetiza en una sola llamada al proveedor de LLM.

    `buscar_y_sintetizar` devuelve un dict con el mismo shape que produce
    el Investigador clásico: {"summary", "key_findings", "sources"}.
    """

    def __init__(self, proveedor: str | None = None):
        self.proveedor = proveedor or proveedor_actual()
        if not soporta_busqueda_nativa(self.proveedor):
            raise NativeSearchNoDisponible(
                f"El proveedor '{self.proveedor}' no tiene búsqueda web nativa. "
                "Usa SEARCH_PROVIDER=tavily o cambia LLM_PROVIDER a claude o gemini."
            )

    def buscar_y_sintetizar(self, objetivo: str, system_prompt: str, max_tokens: int = 8000) -> dict:
        if self.proveedor == "claude":
            texto, fuentes = self._claude(objetivo, system_prompt, max_tokens)
        else:
            texto, fuentes = self._gemini(objetivo, system_prompt, max_tokens)

        datos = self._parsear(texto)
        # Las fuentes que reporta el proveedor son más fiables que las que
        # el modelo escriba en su JSON: vienen de las búsquedas que
        # realmente ejecutó, no de lo que recuerde haber leído.
        if fuentes:
            datos["sources"] = fuentes
        return datos

    # --- Anthropic ---------------------------------------------------------

    def _claude(self, objetivo: str, system_prompt: str, max_tokens: int):
        import anthropic

        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise NativeSearchNoDisponible("ANTHROPIC_API_KEY no está configurada.")

        cliente = anthropic.Anthropic(api_key=key)
        modelo = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

        respuesta = cliente.messages.create(
            model=modelo,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=[
                {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 4},
            ],
            messages=[{"role": "user", "content": objetivo}],
        )

        partes: list[str] = []
        fuentes: list[dict] = []
        for bloque in respuesta.content:
            tipo = getattr(bloque, "type", "")
            if tipo == "text":
                partes.append(bloque.text)
            elif tipo == "web_search_tool_result":
                # En error, `content` es un objeto; en éxito, una lista.
                contenido = getattr(bloque, "content", None)
                if isinstance(contenido, list):
                    for r in contenido:
                        url = getattr(r, "url", None)
                        if url:
                            fuentes.append({"title": getattr(r, "title", "") or url, "url": url})
                else:
                    logger.warning(f"Búsqueda web devolvió error: {contenido}")

        return "\n".join(partes), self._unicas(fuentes)

    # --- Gemini ------------------------------------------------------------

    def _gemini(self, objetivo: str, system_prompt: str, max_tokens: int):
        from google import genai
        from google.genai import types

        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise NativeSearchNoDisponible("GEMINI_API_KEY no está configurada.")

        cliente = genai.Client(api_key=key)
        modelo = os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

        respuesta = cliente.models.generate_content(
            model=modelo,
            contents=objetivo,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                # Con grounding NO se puede pedir response_mime_type JSON:
                # la API lo rechaza. Por eso el parseo tolera texto con el
                # JSON embebido, en vez de exigir JSON puro.
                tools=[types.Tool(google_search=types.GoogleSearch())],
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )

        fuentes: list[dict] = []
        for candidato in getattr(respuesta, "candidates", None) or []:
            meta = getattr(candidato, "grounding_metadata", None)
            for trozo in (getattr(meta, "grounding_chunks", None) or []) if meta else []:
                web = getattr(trozo, "web", None)
                if not web or not getattr(web, "uri", None):
                    continue
                titulo = getattr(web, "title", "") or ""
                fuentes.append({"title": titulo or web.uri, "url": self._url_real(titulo, web.uri)})

        return (respuesta.text or ""), self._unicas(fuentes)

    @staticmethod
    def _url_real(titulo: str, uri: str) -> str:
        """
        Convierte la URL de redirección de Google en la del sitio real.

        El grounding de Gemini no devuelve la URL original, sino un enlace
        opaco a `vertexaisearch.cloud.google.com/grounding-api-redirect/...`.
        Eso rompe dos cosas río abajo:

          - El Enriquecedor no puede visitar el sitio de la empresa.
          - La memoria deduplica por dominio, así que TODAS las entidades
            colapsarían en una sola bajo el dominio de Google.

        Por suerte el `title` del trozo suele ser el dominio ("mps.com.co"),
        así que se reconstruye la URL real desde ahí. Si el título no
        parece un dominio, se conserva el redirect: al menos sigue siendo
        una fuente verificable a mano.
        """
        limpio = (titulo or "").strip().lower()
        if (
            limpio
            and " " not in limpio
            and "." in limpio
            and not limpio.startswith(("http://", "https://"))
            and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", limpio)
        ):
            return f"https://{limpio}"
        return uri

    # --- Auxiliares ---------------------------------------------------------

    @staticmethod
    def _unicas(fuentes: list[dict]) -> list[dict]:
        vistas, salida = set(), []
        for f in fuentes:
            if f["url"] in vistas:
                continue
            vistas.add(f["url"])
            salida.append(f)
        return salida

    @staticmethod
    def _parsear(texto: str) -> dict:
        """
        Extrae el JSON de la respuesta. Con búsqueda nativa el modelo suele
        acompañar el JSON de texto explicativo, así que no se puede exigir
        JSON puro: se busca el primer objeto balanceado.
        """
        limpio = (texto or "").strip()
        if limpio.startswith("```"):
            limpio = limpio.strip("`").removeprefix("json").strip()

        try:
            return json.loads(limpio)
        except json.JSONDecodeError:
            pass

        inicio = limpio.find("{")
        if inicio != -1:
            profundidad = 0
            for i in range(inicio, len(limpio)):
                if limpio[i] == "{":
                    profundidad += 1
                elif limpio[i] == "}":
                    profundidad -= 1
                    if profundidad == 0:
                        try:
                            return json.loads(limpio[inicio : i + 1])
                        except json.JSONDecodeError:
                            break

        # Sin JSON utilizable, se devuelve el texto como resumen en vez de
        # fallar: una síntesis en prosa sigue siendo útil para el usuario.
        logger.warning("La búsqueda nativa no devolvió JSON; se usa el texto como resumen")
        return {"summary": limpio[:6000], "key_findings": [], "sources": []}
