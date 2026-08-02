"""
Factory de LLMs.

Soporta tres proveedores: Anthropic Claude (por defecto), OpenAI y Google
Gemini. El proveedor activo se elige con la variable de entorno
`LLM_PROVIDER`; los agentes llaman a `get_llm_client()` sin argumentos y
reciben el que esté configurado, sin conocer cuál es.

Cada cliente importa su SDK DENTRO de `__init__`, no arriba del módulo:
así, tener solo una de las tres librerías instaladas no rompe el worker
mientras no selecciones ese proveedor.

Los identificadores de modelo son configurables por variable de entorno
(`ANTHROPIC_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`) porque cambian con
más frecuencia que el código. Los valores por defecto son los modelos
vigentes a julio de 2026; si un proveedor retira uno, se ajusta sin tocar
este archivo.
"""
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("utils.llm_factory")

DEFAULT_PROVIDER = "claude"
SUPPORTED_PROVIDERS = ("claude", "openai", "gemini")


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False
    ) -> str:
        """
        Devuelve solo el texto de la respuesta del modelo.

        `json_mode=True` activa la salida JSON nativa del proveedor cuando
        existe. Es bastante más fiable que pedirlo en el prompt: el modelo
        no puede devolver texto suelto ni backticks, y no corta la
        respuesta a media cadena. Úsalo en toda llamada cuya salida se
        vaya a pasar por `json.loads`.
        """
        ...


def _require_key(env_var: str, provider: str) -> str:
    key = os.getenv(env_var)
    if not key:
        raise RuntimeError(
            f"LLM_PROVIDER='{provider}' pero {env_var} no está configurada. "
            f"Define {env_var} en tu .env, o cambia LLM_PROVIDER a otro de: "
            f"{', '.join(SUPPORTED_PROVIDERS)}."
        )
    return key


class ClaudeClient(LLMClient):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        import anthropic

        key = api_key or _require_key("ANTHROPIC_API_KEY", "claude")
        self.client = anthropic.Anthropic(api_key=key)
        # `or` en vez de un default de os.getenv: Compose pasa las
        # variables no definidas como cadena VACÍA, no ausentes, y
        # os.getenv(x, "default") solo usa el default si falta del todo.
        self.model = model or os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

    def complete(
        self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False
    ) -> str:
        # Anthropic no tiene un flag de "solo JSON"; la vía equivalente es
        # instruirlo en el prompt, que es lo que ya hacen los agentes.
        # `json_mode` se acepta para respetar la interfaz y se ignora.
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise RuntimeError(
                f"La respuesta se cortó al alcanzar el límite de {max_tokens} tokens. "
                "Sube max_tokens o recorta el material de entrada."
            )
        # La respuesta puede tener varios bloques; nos interesa el texto.
        text_blocks = [block.text for block in response.content if block.type == "text"]
        return "\n".join(text_blocks)


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from openai import OpenAI

        key = api_key or _require_key("OPENAI_API_KEY", "openai")
        self.client = OpenAI(api_key=key)
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4o"

    def complete(
        self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False
    ) -> str:
        # El prompt de sistema va como primer mensaje con role="system":
        # es el equivalente al parámetro `system` de Anthropic.
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **extra,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError(
                f"La respuesta se cortó al alcanzar el límite de {max_tokens} tokens. "
                "Sube max_tokens o recorta el material de entrada."
            )
        return choice.message.content or ""


class GeminiClient(LLMClient):
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai
        from google.genai import types

        self._types = types
        key = api_key or _require_key("GEMINI_API_KEY", "gemini")
        self.client = genai.Client(api_key=key)
        self.model = model or os.getenv("GEMINI_MODEL") or "gemini-2.5-flash"

    def complete(
        self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False
    ) -> str:
        # Gemini no tiene un rol "system" en `contents`: la instrucción de
        # sistema va aparte, en la configuración de la petición.
        opciones = {
            "system_instruction": system,
            "max_output_tokens": max_tokens,
            # Los modelos 2.5 razonan por defecto, y esos tokens de
            # razonamiento SE DESCUENTAN de max_output_tokens. Con
            # respuestas JSON largas eso agotaba el presupuesto antes de
            # terminar de escribir el objeto, y llegaba cortado a media
            # cadena ("Unterminated string"). Estas llamadas son de
            # extracción y formateo estructurado, no de razonamiento
            # abierto, así que el razonamiento se desactiva y todo el
            # presupuesto va a la respuesta.
            "thinking_config": self._types.ThinkingConfig(thinking_budget=0),
        }
        if json_mode:
            opciones["response_mime_type"] = "application/json"

        response = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=self._types.GenerateContentConfig(**opciones),
        )

        # Si el modelo se detuvo por límite de tokens, la respuesta está
        # incompleta: es mejor decirlo que devolver un JSON roto que
        # falle más adelante con un error sin relación con la causa.
        candidatos = getattr(response, "candidates", None) or []
        if candidatos:
            razon = str(getattr(candidatos[0], "finish_reason", "") or "")
            if "MAX_TOKENS" in razon:
                raise RuntimeError(
                    f"La respuesta se cortó al alcanzar el límite de {max_tokens} tokens. "
                    "Sube max_tokens o recorta el material de entrada."
                )

        return response.text or ""


_CLIENTS: dict[str, type[LLMClient]] = {
    "claude": ClaudeClient,
    "openai": OpenAIClient,
    "gemini": GeminiClient,
}


def get_llm_client(provider: str | None = None) -> LLMClient:
    """
    Devuelve el cliente del proveedor pedido.

    Sin argumento (el caso normal en los agentes), usa `LLM_PROVIDER` del
    entorno y, si tampoco está definida, cae a Claude.
    """
    name = (provider or os.getenv("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()

    if name not in _CLIENTS:
        raise ValueError(
            f"Proveedor de LLM no soportado: '{name}'. "
            f"Valores válidos para LLM_PROVIDER: {', '.join(SUPPORTED_PROVIDERS)}."
        )

    logger.info(f"Usando proveedor de LLM: {name}")
    return _CLIENTS[name]()
