"""
Agente Verificador — contrasta el resultado contra lo que se pidió.

Hasta ahora nadie comprobaba si el objetivo se cumplió: el Director
consolidaba lo que hubiera y devolvía COMPLETED aunque el usuario hubiera
pedido 50 proveedores y se hubieran encontrado 6.

El Verificador responde tres cosas:
  - ¿cumple el objetivo? (booleano)
  - ¿qué tan cerca quedó? (puntaje 0.0–1.0)
  - ¿qué falta, y qué subtarea lo resolvería?

Ese último punto es el que habilita la replanificación: cada hueco trae
una `accion_sugerida` y un `agente_sugerido`, que el Director convierte
en subtareas nuevas para una ronda adicional.

Dos comprobaciones se hacen en código, no con el LLM, porque son
objetivas y gratis: contar filas contra la cantidad pedida, y contar
artefactos cuando se pidió un archivo. Un modelo puede decir que "se
cumplió" mirando un CSV de 6 filas cuando se pidieron 50.
"""
import json
import logging
import re

from agents.base import BaseAgent
from models import (
    TaskJob,
    TaskResult,
    TaskStatus,
    TaskType,
    VerificationGap,
    VerificationOutput,
)
from utils.llm_factory import get_llm_client

logger = logging.getLogger("verifier")

MAX_CONTEXT_CHARS = 18000

SYSTEM_PROMPT = """Eres un Verificador de calidad. Recibes el OBJETIVO \
original de un usuario y el RESULTADO que produjo un equipo de agentes. \
Tu trabajo es decidir si el resultado cumple el objetivo, y si no, decir \
exactamente qué falta.

Sé estricto y concreto. No des por bueno un resultado que se quedó en \
generalidades cuando se pidieron datos específicos, ni uno que entregó \
menos elementos de los pedidos.

Para cada hueco que encuentres, propón UNA acción concreta que lo \
resuelva, redactada como instrucción autocontenida para un agente, e \
indica qué agente debería ejecutarla:
- RESEARCH: falta información que hay que buscar en la web.
- EXTRACTION: la información está en el material pero no se estructuró.
- ANALYSIS: hay que calcular, consolidar o generar un CSV.
- DOCUMENT: falta el informe en Word.
- SPREADSHEET: falta el libro de Excel.
- PRESENTATION: falta la presentación.
- WEBSITE: falta el sitio web.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto, sin texto \
adicional y sin backticks de markdown:

{
  "cumple": true|false,
  "puntaje": 0.0,
  "resumen": "string: en una o dos frases, qué se logró y qué no",
  "huecos": [
    {"descripcion": "...", "accion_sugerida": "...", "agente_sugerido": "RESEARCH"}
  ]
}

Si el resultado cumple del todo, devuelve "cumple": true, "puntaje": 1.0 \
y "huecos": []."""


class VerifierAgent(BaseAgent):
    name = "verifier"

    def __init__(self):
        self.llm = get_llm_client()

    def run(self, job: TaskJob) -> TaskResult:
        """Permite invocarlo también como tarea suelta (type=VERIFICATION)."""
        objetivo = job.metadata.get("objetivo") or job.prompt
        resultado = job.metadata.get("context_data") or {}
        veredicto = self.verificar(objetivo, resultado, artefactos=job.metadata.get("artefactos") or [])
        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result=veredicto.model_dump(),
        )

    # --- API que usa el Director -------------------------------------------

    def verificar(self, objetivo: str, resultado: dict, artefactos: list) -> VerificationOutput:
        objetivos_duros = self._comprobaciones_objetivas(objetivo, resultado, artefactos)

        try:
            veredicto = self._veredicto_llm(objetivo, resultado)
        except Exception as exc:
            logger.exception("El Verificador falló consultando al LLM")
            # Sin veredicto del modelo, se devuelve lo que sí se pudo
            # comprobar en código. Nunca se bloquea la tarea por esto.
            return VerificationOutput(
                cumple=not objetivos_duros,
                puntaje=0.0 if objetivos_duros else 1.0,
                resumen=f"Verificación automática parcial (el modelo no respondió: {exc}).",
                huecos=objetivos_duros,
            )

        # Las comprobaciones objetivas mandan sobre la opinión del modelo:
        # si faltan filas o artefactos, no cumple, diga lo que diga.
        if objetivos_duros:
            veredicto.cumple = False
            veredicto.puntaje = min(veredicto.puntaje, 0.6)
            ya = {h.descripcion for h in veredicto.huecos}
            veredicto.huecos.extend(h for h in objetivos_duros if h.descripcion not in ya)

        logger.info(
            f"Veredicto: cumple={veredicto.cumple} puntaje={veredicto.puntaje} "
            f"huecos={len(veredicto.huecos)}"
        )
        return veredicto

    # --- Internos -----------------------------------------------------------

    def _veredicto_llm(self, objetivo: str, resultado: dict) -> VerificationOutput:
        texto = json.dumps(resultado, ensure_ascii=False, indent=1)
        if len(texto) > MAX_CONTEXT_CHARS:
            texto = texto[:MAX_CONTEXT_CHARS] + "\n…(recortado)"

        raw = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=f"OBJETIVO ORIGINAL:\n{objetivo}\n\nRESULTADO OBTENIDO:\n{texto}",
            max_tokens=2000,
            json_mode=True,
        )
        limpio = raw.strip()
        if limpio.startswith("```"):
            limpio = limpio.strip("`").removeprefix("json").strip()
        return VerificationOutput.model_validate(json.loads(limpio))

    @staticmethod
    def _cantidad_pedida(objetivo: str) -> int | None:
        """
        Extrae "los 50 proveedores" -> 50. Solo cuenta números que
        preceden a un sustantivo, para no confundirse con años o precios.
        """
        m = re.search(
            r"\b(\d{1,4})\s+(?!de\b|del\b|año|años|mil|millones)([a-záéíóúñ]{4,})",
            objetivo.lower(),
        )
        if not m:
            return None
        n = int(m.group(1))
        return n if 2 <= n <= 1000 else None

    @classmethod
    def _comprobaciones_objetivas(
        cls, objetivo: str, resultado: dict, artefactos: list
    ) -> list[VerificationGap]:
        """Comprobaciones deterministas: sin LLM, sin costo, sin opinión."""
        huecos: list[VerificationGap] = []

        # 1) ¿Se pidió una cantidad concreta y se entregó menos?
        pedidas = cls._cantidad_pedida(objetivo)
        obtenidas = cls._contar_filas(resultado)
        if pedidas and obtenidas is not None and obtenidas < pedidas:
            huecos.append(
                VerificationGap(
                    descripcion=(
                        f"Se pidieron {pedidas} registros y solo se obtuvieron {obtenidas}."
                    ),
                    accion_sugerida=(
                        f"Busca {pedidas - obtenidas} registros adicionales que cumplan el objetivo "
                        f"'{objetivo[:150]}', distintos de los ya encontrados."
                    ),
                    agente_sugerido=TaskType.RESEARCH,
                )
            )

        # 2) ¿Se pidió un archivo y no se generó ninguno?
        formatos = {
            "csv": TaskType.ANALYSIS,
            "excel": TaskType.SPREADSHEET,
            "xlsx": TaskType.SPREADSHEET,
            "word": TaskType.DOCUMENT,
            "informe": TaskType.DOCUMENT,
            "presentación": TaskType.PRESENTATION,
            "presentacion": TaskType.PRESENTATION,
        }
        bajo = objetivo.lower()
        for palabra, agente in formatos.items():
            if palabra in bajo and not artefactos:
                huecos.append(
                    VerificationGap(
                        descripcion=f"Se pidió un archivo ({palabra}) y no se generó ninguno.",
                        accion_sugerida=(
                            f"Genera el archivo {palabra} solicitado a partir de los datos ya "
                            "recopilados por las subtareas anteriores."
                        ),
                        agente_sugerido=agente,
                    )
                )
                break

        return huecos

    @staticmethod
    def _contar_filas(resultado: dict) -> int | None:
        """Busca la tabla más grande dentro del resultado consolidado."""
        maximo = None
        pila = [resultado]
        while pila:
            actual = pila.pop()
            if isinstance(actual, dict):
                filas = actual.get("rows")
                if isinstance(filas, list):
                    maximo = max(maximo or 0, len(filas))
                if isinstance(actual.get("csv_rows"), int):
                    maximo = max(maximo or 0, actual["csv_rows"])
                pila.extend(v for v in actual.values() if isinstance(v, (dict, list)))
            elif isinstance(actual, list):
                pila.extend(v for v in actual if isinstance(v, (dict, list)))
        return maximo
