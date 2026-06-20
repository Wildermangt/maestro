"""
Agente Analista — versión básica (Sprint 3).

Flujo:
1. Pide a Claude que genere código Python (pandas/stdlib) que produzca
   los KPIs/hallazgos relevantes para el prompt, e imprima un JSON
   por stdout con el shape esperado.
2. Ejecuta ese código en el sandbox (tools/code_executor.py).
3. Si el código falla, reintenta UNA vez pidiéndole a Claude que
   corrija el error (el stderr se le pasa de vuelta).
4. Parsea el stdout como AnalysisOutput.

Esto es deliberadamente más simple que el diseño final del documento
original (sin Docker real, sin DuckDB, sin Plotly todavía) — es la
versión mínima que ya prueba el patrón "LLM genera código -> sandbox
ejecuta -> resultado estructurado", que es el riesgo técnico real de
este agente. Subir a sandbox Docker + gráficos es trabajo de Sprint 4.
"""
import json
import logging

from agents.base import BaseAgent
from models import AnalysisOutput, TaskJob, TaskResult, TaskStatus
from tools.code_executor import CodeExecutorTool
from utils.llm_factory import get_llm_client

logger = logging.getLogger("analyst")

MAX_RETRIES = 1

SYSTEM_PROMPT = """Eres un Analista de datos experto en Python (pandas, \
estadística básica). Recibes un objetivo de análisis y, si está disponible, \
datos de contexto (de una investigación previa).

Tu trabajo: escribir un script Python AUTOCONTENIDO que:
1. No lea archivos externos ni haga peticiones de red (no hay datos\
 reales de archivo disponibles — si necesitas datos de ejemplo, \
 constrúyelos tú mismo de forma razonable dentro del script, dejando \
 claro en el resumen que son ilustrativos si no vienen de la investigación).
2. Calcule métricas/KPIs relevantes al objetivo.
3. Imprima por stdout, como ÚNICA salida, un objeto JSON con este shape \
exacto (nada de texto antes o después, sin backticks de markdown):

{
  "summary": "string explicando qué se calculó y qué significa",
  "metrics": {"nombre_metrica": valor_numerico_o_string, ...},
  "insights": ["string", "string", ...]
}

Solo puedes usar la librería estándar de Python y pandas (ya están \
disponibles, no instales nada con pip)."""

RETRY_PROMPT_TEMPLATE = """El script anterior falló al ejecutarse. \
Corrígelo. Aquí está el código que escribiste:

{code}

Y este fue el error:

{error}

Devuelve el script corregido completo (no solo el fragmento con el error), \
siguiendo las mismas reglas del mensaje anterior."""


class AnalystAgent(BaseAgent):
    name = "analyst"

    def __init__(self):
        self.executor = CodeExecutorTool()
        self.llm = get_llm_client(provider="claude")

    def run(self, job: TaskJob) -> TaskResult:
        prompt = job.prompt
        context_data = job.metadata.get("context_data")  # ej: hallazgos del Investigador
        logger.info(f"AnalystAgent procesando taskId={job.taskId} prompt='{prompt}'")

        user_message = f"OBJETIVO DE ANÁLISIS: {prompt}"
        if context_data:
            user_message += f"\n\nDATOS DE CONTEXTO (de investigación previa):\n{json.dumps(context_data, ensure_ascii=False)}"

        code = self.llm.complete(system=SYSTEM_PROMPT, user=user_message, max_tokens=2000)
        code = self._strip_markdown_fence(code)

        for attempt in range(MAX_RETRIES + 1):
            result = self.executor.run(code)

            if result.success:
                try:
                    parsed = json.loads(result.stdout.strip())
                    output = AnalysisOutput.model_validate(parsed)
                    return TaskResult(
                        taskId=job.taskId,
                        status=TaskStatus.COMPLETED,
                        result=output.model_dump(),
                    )
                except Exception as exc:
                    logger.warning(f"Código ejecutó OK pero stdout no es el JSON esperado: {exc}")
                    result.success = False
                    result.stderr = f"El stdout no es JSON válido con el shape esperado: {exc}\nstdout fue: {result.stdout[:500]}"

            if attempt < MAX_RETRIES:
                logger.info(f"Intento {attempt + 1} falló, pidiendo corrección al LLM. stderr: {result.stderr[:300]}")
                retry_message = RETRY_PROMPT_TEMPLATE.format(code=code, error=result.stderr)
                code = self.llm.complete(system=SYSTEM_PROMPT, user=retry_message, max_tokens=2000)
                code = self._strip_markdown_fence(code)

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.FAILED,
            result={"lastCode": code},
            error=f"El análisis falló tras {MAX_RETRIES + 1} intento(s). Último error: {result.stderr}",
        )

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:] if lines[0].startswith("```") else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        return cleaned
