"""
Agente Documento — genera un informe en Word (.docx).

Mismo patrón que el Presentador: el LLM produce la ESTRUCTURA en JSON y
python-docx construye el archivo real. El modelo nunca genera el binario
ni código que lo escriba; solo contenido, que se valida con Pydantic
antes de tocar el disco.
"""
import io
import json
import logging

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from agents.base import BaseAgent
from models import DocumentOutput, TaskJob, TaskResult, TaskStatus
from utils.artifact_storage import save_artifact
from utils.llm_factory import get_llm_client

logger = logging.getLogger("writer")

MAX_CONTEXT_CHARS = 20000

SYSTEM_PROMPT = """Eres un redactor técnico. Recibes un objetivo y, si \
está disponible, material de contexto de investigaciones previas. \
Produce la estructura de un informe profesional.

Reglas:
- Entre 3 y 8 secciones, con títulos concretos (no "Introducción" a secas \
si puedes ser más específico).
- Cada sección: 1 a 3 párrafos de texto corrido, redactados, no viñetas \
sueltas. Escribe en el idioma del objetivo.
- Usa ÚNICAMENTE datos que estén en el material de contexto. Si no hay \
material, escribe el informe sin cifras inventadas.
- Si el material tiene fuentes con URL, inclúyelas en una sección final.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto, sin texto \
adicional y sin backticks de markdown:

{
  "title": "string",
  "subtitle": "string o cadena vacía",
  "sections": [{"heading": "string", "paragraphs": ["string", "string"]}]
}"""


class WriterAgent(BaseAgent):
    name = "writer"

    def __init__(self):
        self.llm = get_llm_client()

    def run(self, job: TaskJob) -> TaskResult:
        contexto = job.metadata.get("context_data")
        logger.info(f"WriterAgent procesando taskId={job.taskId} prompt='{job.prompt[:80]}'")

        user_message = f"OBJETIVO DEL INFORME:\n{job.prompt}"
        if contexto:
            texto = json.dumps(contexto, ensure_ascii=False, indent=1)[:MAX_CONTEXT_CHARS]
            user_message += f"\n\nMATERIAL DE CONTEXTO:\n{texto}"

        try:
            raw = self.llm.complete(system=SYSTEM_PROMPT, user=user_message, max_tokens=6000, json_mode=True)
            limpio = raw.strip()
            if limpio.startswith("```"):
                limpio = limpio.strip("`").removeprefix("json").strip()
            plan = json.loads(limpio)
        except Exception as exc:
            logger.exception(f"No se pudo generar la estructura del informe para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"No se pudo generar la estructura del informe: {exc}",
            )

        try:
            docx_bytes = self._construir(plan)
        except Exception as exc:
            logger.exception(f"python-docx falló construyendo el informe para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={"plan": plan},
                error=f"La estructura se generó pero el archivo .docx falló: {exc}",
            )

        artefacto = save_artifact(
            task_id=job.taskId,
            filename="informe.docx",
            content_bytes=docx_bytes,
            artifact_type="docx",
        )

        secciones = plan.get("sections") or []
        salida = DocumentOutput(
            summary=f"Informe '{plan.get('title', 'sin título')}' con {len(secciones)} sección(es).",
            section_count=len(secciones),
        )
        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result=salida.model_dump(),
            artifacts=[artefacto.to_dict()],
        )

    @staticmethod
    def _construir(plan: dict) -> bytes:
        doc = Document()

        estilo = doc.styles["Normal"]
        estilo.font.name = "Calibri"
        estilo.font.size = Pt(11)

        titulo = doc.add_heading(str(plan.get("title") or "Informe"), level=0)
        titulo.alignment = WD_ALIGN_PARAGRAPH.CENTER

        subtitulo = str(plan.get("subtitle") or "").strip()
        if subtitulo:
            p = doc.add_paragraph(subtitulo)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.runs[0].italic = True

        for seccion in plan.get("sections") or []:
            doc.add_heading(str(seccion.get("heading") or ""), level=1)
            for parrafo in seccion.get("paragraphs") or []:
                doc.add_paragraph(str(parrafo))

        buffer = io.BytesIO()
        doc.save(buffer)
        return buffer.getvalue()
