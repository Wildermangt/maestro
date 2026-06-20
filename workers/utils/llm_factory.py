"""
Factory de LLMs. Hoy solo implementa Anthropic Claude (decisión del
Sprint 2), pero la interfaz LLMClient está pensada para que añadir
OpenAI o Gemini más adelante sea implementar una clase nueva, sin
tocar el código de los agentes que la consumen.
"""
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("utils.llm_factory")


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:
        """Devuelve solo el texto de la respuesta del modelo."""
        ...


class ClaudeClient(LLMClient):
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6"):
        import anthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY no está configurada. "
                "Define la variable de entorno antes de iniciar el worker."
            )
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model

    def complete(self, system: str, user: str, max_tokens: int = 2000) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # La respuesta puede tener varios bloques; nos interesa el texto.
        text_blocks = [block.text for block in response.content if block.type == "text"]
        return "\n".join(text_blocks)


def get_llm_client(provider: str = "claude") -> LLMClient:
    if provider == "claude":
        return ClaudeClient()
    raise ValueError(f"Proveedor de LLM no soportado: {provider}")
