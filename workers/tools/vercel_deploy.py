"""
Cliente mínimo de la API de Deployments de Vercel.
Docs: https://vercel.com/docs/rest-api/endpoints/deployments

Requiere VERCEL_TOKEN en el entorno. Sube un sitio estático simple
(un único index.html, sin build step) como un nuevo deployment.
"""
import logging
import os

import requests

logger = logging.getLogger("tools.vercel_deploy")

VERCEL_API_BASE = "https://api.vercel.com"


class VercelDeployTool:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("VERCEL_TOKEN")
        if not self.api_key:
            raise RuntimeError(
                "VERCEL_TOKEN no está configurada. "
                "Define la variable de entorno antes de iniciar el worker."
            )

    def deploy_static_site(self, project_name: str, html_content: str) -> str:
        """
        Crea un deployment de un único archivo index.html.
        Devuelve la URL pública del deployment si tiene éxito.
        Lanza una excepción si Vercel rechaza la petición — el agente
        que llama debe capturarla y degradar (ej: devolver el HTML sin
        deploy en vez de fallar la tarea completa).
        """
        url = f"{VERCEL_API_BASE}/v13/deployments"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        payload = {
            "name": project_name,
            "files": [{"file": "index.html", "data": html_content}],
            "target": "production",
            "projectSettings": {"framework": None},
        }

        logger.info(f"Desplegando sitio '{project_name}' a Vercel")
        response = requests.post(url, json=payload, headers=headers, timeout=30)

        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Vercel respondió {response.status_code}: {response.text[:500]}"
            )

        data = response.json()
        deploy_url = data.get("url")
        if not deploy_url:
            raise RuntimeError(f"Vercel no devolvió una URL de deployment: {data}")

        full_url = f"https://{deploy_url}"
        logger.info(f"Deploy exitoso: {full_url}")
        return full_url
