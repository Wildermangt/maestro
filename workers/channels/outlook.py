"""
Canal de Outlook (Sprint 6) — lectura por polling + envío, vía Microsoft
Graph API y OAuth2. Mismo diseño que channels/gmail.py: no encola jobs
directo en Redis, crea tareas vía POST /api/tasks del backend.

A diferencia de Gmail (que usa la librería oficial de Google), aquí se
habla directo con la REST API de Microsoft Graph vía `requests` — es
más simple que traer el SDK completo de Microsoft solo para leer y
enviar correo, y mantiene el mismo patrón ya usado en el resto del
proyecto (Vercel, Firecrawl, Tavily ya hablan así, sin SDK dedicado).

Autenticación: OAuth2 con refresh token (ver
channels/oauth_scripts/outlook_authorize.py). El access token se
renueva manualmente en cada ciclo de polling si expiró — Microsoft no
tiene una librería que lo haga automático como google-auth, así que se
maneja explícitamente aquí.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("channels.outlook")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
SCOPES = "Mail.Read Mail.Send offline_access"
DEFAULT_POLL_INTERVAL_SECONDS = 60


class OutlookChannel:
    def __init__(
        self,
        client_id: str | None = None,
        refresh_token: str | None = None,
        allowed_senders: list[str] | None = None,
        api_base_url: str | None = None,
        poll_interval_seconds: int | None = None,
    ):
        self.client_id = client_id or os.getenv("OUTLOOK_CLIENT_ID")
        self.refresh_token = refresh_token or os.getenv("OUTLOOK_REFRESH_TOKEN")
        self.api_base_url = api_base_url or os.getenv("BACKEND_INTERNAL_URL", "http://backend:4000")
        self.poll_interval_seconds = poll_interval_seconds or int(
            os.getenv("OUTLOOK_POLL_INTERVAL_SECONDS") or DEFAULT_POLL_INTERVAL_SECONDS
        )

        if not all([self.client_id, self.refresh_token]):
            raise RuntimeError(
                "OUTLOOK_CLIENT_ID y OUTLOOK_REFRESH_TOKEN son obligatorias. "
                "Genera el refresh token con "
                "channels/oauth_scripts/outlook_authorize.py antes de habilitar este canal."
            )

        raw_senders = allowed_senders if allowed_senders is not None else os.getenv("OUTLOOK_ALLOWED_SENDERS", "")
        self.allowed_senders = (
            [s.strip().lower() for s in raw_senders if s.strip()]
            if isinstance(raw_senders, list)
            else [s.strip().lower() for s in raw_senders.split(",") if s.strip()]
        )
        if not self.allowed_senders:
            raise RuntimeError(
                "OUTLOOK_ALLOWED_SENDERS no está configurada (lista separada "
                "por comas de remitentes permitidos). Sin esto, cualquier "
                "correo entrante crearía tareas — riesgo de abuso inaceptable."
            )

        self._access_token: str | None = None
        self._last_check_time: str | None = None  # ISO 8601, filtro receivedDateTime
        # NOTA: _access_token se lee/escribe desde dos hilos (el loop de
        # polling en run_forever, y el hilo principal del worker al llamar
        # send_message para notificar resultados). Para el volumen de uso
        # de un solo usuario esto no genera problemas prácticos — en el
        # peor caso, una llamada usa un token recién vencido y Microsoft
        # responde 401, lo cual ya se loggea como excepción sin tumbar el
        # worker. Si esto se usa con múltiples usuarios concurrentes,
        # vale la pena agregar un lock explícito aquí.

    def run_forever(self) -> None:
        logger.info(
            f"Canal de Outlook iniciado. Polling cada {self.poll_interval_seconds}s, "
            f"remitentes permitidos: {self.allowed_senders}"
        )
        # Ancla el punto de partida a "ahora" — el primer ciclo no debe
        # procesar correo histórico, solo lo que llegue después de arrancar.
        from datetime import datetime, timezone

        self._last_check_time = datetime.now(timezone.utc).isoformat()

        while True:
            try:
                self._refresh_access_token()
                self._check_new_messages()
            except requests.exceptions.RequestException:
                logger.exception("Error de red con Microsoft Graph, reintentando en el siguiente ciclo")
            except Exception:
                logger.exception("Error inesperado en el loop de Outlook, continuando")
            time.sleep(self.poll_interval_seconds)

    def _refresh_access_token(self) -> None:
        response = requests.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "scope": SCOPES,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]

        # Microsoft puede rotar el refresh token en cada uso — si viene
        # uno nuevo, lo adoptamos en memoria. No persiste entre reinicios
        # del contenedor (eso requeriría escribir de vuelta al .env, que
        # no hacemos); si el token rota y el contenedor se reinicia antes
        # de que actualices el .env manualmente, deberás re-autorizar.
        # Aceptable para este alcance — ver limitación documentada abajo.
        if "refresh_token" in data and data["refresh_token"] != self.refresh_token:
            logger.info("Microsoft rotó el refresh token; se usa el nuevo solo en memoria para esta sesión")
            self.refresh_token = data["refresh_token"]

    def _check_new_messages(self) -> None:
        headers = {"Authorization": f"Bearer {self._access_token}"}
        response = requests.get(
            f"{GRAPH_API_BASE}/me/mailFolders/inbox/messages",
            headers=headers,
            params={
                "$filter": f"receivedDateTime gt {self._last_check_time}",
                "$select": "id,from,subject,bodyPreview,receivedDateTime",
                "$orderby": "receivedDateTime asc",
                "$top": 25,
            },
            timeout=15,
        )
        response.raise_for_status()
        messages = response.json().get("value", [])

        for message in messages:
            self._process_message(message, headers)

        if messages:
            self._last_check_time = messages[-1]["receivedDateTime"]

    def _process_message(self, message: dict, headers: dict) -> None:
        sender_email = (
            message.get("from", {}).get("emailAddress", {}).get("address", "").strip().lower()
        )
        subject = message.get("subject", "(sin asunto)")

        if sender_email not in self.allowed_senders:
            logger.info(f"Correo ignorado, remitente no autorizado: {sender_email}")
            return

        body = self._get_message_body(message["id"], headers)
        prompt = f"{subject}\n\n{body}".strip()

        logger.info(f"Correo autorizado recibido de {sender_email}, creando tarea")
        self._create_task_from_email(prompt, sender_email)

    def _get_message_body(self, message_id: str, headers: dict) -> str:
        response = requests.get(
            f"{GRAPH_API_BASE}/me/messages/{message_id}",
            headers=headers,
            params={"$select": "body"},
            timeout=15,
        )
        response.raise_for_status()
        body_data = response.json().get("body", {})

        content = body_data.get("content", "")
        if body_data.get("contentType") == "html":
            return self._strip_html(content)
        return content

    @staticmethod
    def _strip_html(html: str) -> str:
        """
        Outlook normalmente devuelve el cuerpo como HTML, no texto plano
        (a diferencia de Gmail, que sí ofrece text/plain directo). Quitamos
        las etiquetas con una regex simple — suficiente para extraer el
        texto del prompt, no se necesita un parser HTML completo aquí.
        """
        import re

        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _create_task_from_email(self, prompt: str, sender_email: str) -> None:
        try:
            response = requests.post(
                f"{self.api_base_url}/api/tasks",
                json={
                    "type": "DIRECTOR",
                    "prompt": prompt,
                    "metadata": {"sourceChannel": "outlook", "sourceAddress": sender_email},
                },
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception(f"Error creando tarea desde correo de {sender_email}")
            self.send_message(
                sender_email,
                "Error al crear tu tarea",
                "No pude crear la tarea a partir de tu correo. Intenta de nuevo más tarde.",
            )

    def send_message(self, to_address: str, subject: str, body: str) -> None:
        try:
            if self._access_token is None:
                # send_message puede llamarse desde el hilo principal del
                # worker (al notificar el resultado de una tarea) antes de
                # que run_forever() haya completado su primer ciclo de
                # polling en su propio hilo — sin esto, _access_token
                # seguiría en None y la llamada fallaría con 401.
                self._refresh_access_token()

            headers = {"Authorization": f"Bearer {self._access_token}"}
            response = requests.post(
                f"{GRAPH_API_BASE}/me/sendMail",
                headers=headers,
                json={
                    "message": {
                        "subject": subject,
                        "body": {"contentType": "Text", "content": body},
                        "toRecipients": [{"emailAddress": {"address": to_address}}],
                    }
                },
                timeout=15,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception(f"No se pudo enviar correo a {to_address}")
