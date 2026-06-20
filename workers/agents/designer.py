"""
Agente Diseñador Web (Sprint 4).

Flujo:
1. Claude genera un sitio estático de una sola página (HTML + CSS
   inline, sin JS de build, sin imágenes generadas — alcance acordado
   para este sprint) a partir del prompt.
2. Si VERCEL_TOKEN está configurada, se intenta el deploy real vía
   tools/vercel_deploy.py.
3. Si el deploy falla o no hay token, el agente NO falla la tarea
   completa — devuelve el HTML generado como artefacto descargable
   (el usuario puede abrirlo localmente o subirlo manualmente),
   marcando `deployed: false`. Generar el sitio y desplegarlo son
   dos capacidades separadas; perder la segunda no invalida la primera.
"""
import logging

from agents.base import BaseAgent
from models import TaskJob, TaskResult, TaskStatus, WebsiteOutput
from tools.vercel_deploy import VercelDeployTool
from utils.artifact_storage import save_artifact
from utils.llm_factory import get_llm_client

logger = logging.getLogger("designer")

SYSTEM_PROMPT = """Eres un Diseñador Web experto en crear sitios estáticos \
de una sola página, modernos y limpios, usando solo HTML y CSS inline \
(sin frameworks, sin JavaScript de build, sin dependencias externas \
salvo Google Fonts si lo consideras necesario).

Genera un documento HTML COMPLETO (con <!DOCTYPE html>, <html>, <head> \
con <style> inline, y <body>) listo para servirse tal cual. Usa un \
diseño responsive simple (flexbox/grid), tipografía cuidada, y una \
paleta de colores coherente con el objetivo descrito.

Responde ÚNICAMENTE con el código HTML completo, sin explicaciones antes \
o después, sin backticks de markdown."""


class DesignerAgent(BaseAgent):
    name = "designer"

    def __init__(self):
        self.llm = get_llm_client(provider="claude")

    def run(self, job: TaskJob) -> TaskResult:
        prompt = job.prompt
        logger.info(f"DesignerAgent procesando taskId={job.taskId} prompt='{prompt}'")

        try:
            html_content = self._generate_html(prompt)
        except Exception as exc:
            logger.exception(f"No se pudo generar el HTML para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"No se pudo generar el sitio: {exc}",
            )

        artifact_ref = save_artifact(
            task_id=job.taskId,
            filename="index.html",
            content_bytes=html_content.encode("utf-8"),
            artifact_type="website",
        )

        deploy_url, deployed = self._try_deploy(job.taskId, html_content)

        output = WebsiteOutput(
            summary=(
                f"Sitio generado y desplegado en {deploy_url}"
                if deployed
                else "Sitio generado (el deploy a Vercel no se realizó — "
                "ver el HTML descargable; revisa VERCEL_TOKEN si esperabas un deploy)."
            ),
            deployUrl=deploy_url,
            deployed=deployed,
        )

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result=output.model_dump(),
            artifacts=[artifact_ref.to_dict()],
        )

    def _generate_html(self, prompt: str) -> str:
        raw = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=f"OBJETIVO DEL SITIO: {prompt}",
            max_tokens=4000,
        )
        return self._strip_markdown_fence(raw)

    def _try_deploy(self, task_id: str, html_content: str) -> tuple[str | None, bool]:
        """
        Devuelve (url, deployed). Nunca lanza — un fallo de deploy se
        degrada a (None, False), nunca tumba la tarea completa, porque
        el archivo HTML ya fue generado y guardado independientemente.
        """
        try:
            deployer = VercelDeployTool()
        except RuntimeError as exc:
            logger.info(f"Deploy a Vercel omitido: {exc}")
            return None, False

        try:
            project_name = f"maestro-{task_id[:8]}"
            url = deployer.deploy_static_site(project_name, html_content)
            return url, True
        except Exception:
            logger.exception(f"El deploy a Vercel falló para taskId={task_id}")
            return None, False

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
