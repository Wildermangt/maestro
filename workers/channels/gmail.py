"""
Canal de Gmail (Sprint 6) — lectura por polling + envío, vía OAuth2.

Mismo patrón de diseño que channels/telegram.py: este canal NO encola
jobs directo en Redis, crea tareas llamando a POST /api/tasks del
backend NestJS (única fuente de verdad). Ver ese archivo para la
justificación completa del patrón.

Autenticación: OAuth2 con refresh token de larga duración (ver
channels/oauth_scripts/gmail_authorize.py para cómo se obtiene UNA VEZ).
El access token de corta duración se renueva automáticamente en cada
request usando el refresh token — la librería de Google maneja esto.

Polling: cada POLL_INTERVAL_SECONDS, revisa si hay mensajes nuevos no
leídos en la bandeja desde la última revisión, usando el historyId de
Gmail (más eficiente que listar todo el inbox cada vez).

Seguridad: solo procesa correos de remitentes en ALLOWED_SENDERS (lista
separada por comas en la env var GMAIL_ALLOWED_SENDERS). Sin esa lista
configurada, el canal se niega a arrancar — leer cualquier correo de
cualquier remitente y crear tareas a partir de eso sería una superficie
de abuso trivial (cualquiera que conozca tu dirección podría hacerte
gastar tokens de LLM, o peor, intentar inyectar instrucciones).
"""
import base64
import logging
import os
import time
from email.mime.text import MIMEText

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger("channels.gmail")

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
DEFAULT_POLL_INTERVAL_SECONDS = 60


class GmailChannel:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        allowed_senders: list[str] | None = None,
        api_base_url: str | None = None,
        poll_interval_seconds: int | None = None,
    ):
        self.client_id = client_id or os.getenv("GMAIL_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("GMAIL_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.getenv("GMAIL_REFRESH_TOKEN")
        self.api_base_url = api_base_url or os.getenv("BACKEND_INTERNAL_URL", "http://backend:4000")
        self.poll_interval_seconds = poll_interval_seconds or int(
            os.getenv("GMAIL_POLL_INTERVAL_SECONDS") or DEFAULT_POLL_INTERVAL_SECONDS
        )

        if not all([self.client_id, self.client_secret, self.refresh_token]):
            raise RuntimeError(
                "GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET y GMAIL_REFRESH_TOKEN son "
                "obligatorias. Genera el refresh token con "
                "channels/oauth_scripts/gmail_authorize.py antes de habilitar este canal."
            )

        raw_senders = allowed_senders if allowed_senders is not None else os.getenv("GMAIL_ALLOWED_SENDERS", "")
        self.allowed_senders = (
            [s.strip().lower() for s in raw_senders if s.strip()]
            if isinstance(raw_senders, list)
            else [s.strip().lower() for s in raw_senders.split(",") if s.strip()]
        )
        if not self.allowed_senders:
            raise RuntimeError(
                "GMAIL_ALLOWED_SENDERS no está configurada (lista separada por "
                "comas de remitentes permitidos). Sin esto, cualquier correo "
                "entrante crearía tareas — riesgo de abuso inaceptable."
            )

        self._credentials = Credentials(
            token=None,
            refresh_token=self.refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=GMAIL_SCOPES,
        )
        self._service = build("gmail", "v1", credentials=self._credentials)
        self._last_history_id: str | None = None

    def run_forever(self) -> None:
        """
        Loop de polling. Corre en un hilo separado (ver main.py), igual
        que el canal de Telegram — así un fallo de red en este canal
        nunca bloquea el procesamiento normal de la cola BullMQ.
        """
        logger.info(
            f"Canal de Gmail iniciado. Polling cada {self.poll_interval_seconds}s, "
            f"remitentes permitidos: {self.allowed_senders}"
        )
        self._init_history_id()

        while True:
            try:
                self._check_new_messages()
            except HttpError:
                logger.exception("Error de la API de Gmail, reintentando en el siguiente ciclo")
            except Exception:
                logger.exception("Error inesperado en el loop de Gmail, continuando")
            time.sleep(self.poll_interval_seconds)

    def _init_history_id(self) -> None:
        """
        Al arrancar, ancla el punto de partida en el historial actual de
        Gmail — así el primer ciclo de polling no procesa años de correo
        viejo, solo lo que llegue DESPUÉS de que el worker arrancó.
        """
        try:
            profile = self._service.users().getProfile(userId="me").execute()
            self._last_history_id = profile.get("historyId")
            logger.info(f"Punto de partida de Gmail history establecido: {self._last_history_id}")
        except HttpError:
            logger.exception("No se pudo obtener el historyId inicial de Gmail")

    def _check_new_messages(self) -> None:
        if self._last_history_id is None:
            self._init_history_id()
            return

        try:
            history_response = (
                self._service.users()
                .history()
                .list(userId="me", startHistoryId=self._last_history_id, historyTypes=["messageAdded"])
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                # El historyId expiró (Gmail solo retiene un rango limitado).
                # Reanclamos al historial actual y seguimos desde ahí —
                # se pierden eventos viejos, pero nunca se rompe el loop.
                logger.warning("historyId expirado, reanclando al historial actual")
                self._init_history_id()
                return
            raise

        history_records = history_response.get("history", [])
        self._last_history_id = history_response.get("historyId", self._last_history_id)

        message_ids = set()
        for record in history_records:
            for added in record.get("messagesAdded", []):
                message_ids.add(added["message"]["id"])

        for message_id in message_ids:
            self._process_message(message_id)

    def _process_message(self, message_id: str) -> None:
        message = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"])
            .execute()
        )

        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "(sin asunto)")

        sender_email = self._extract_email(sender)
        if sender_email not in self.allowed_senders:
            logger.info(f"Correo ignorado, remitente no autorizado: {sender_email}")
            return

        body = self._get_message_body(message_id)
        prompt = f"{subject}\n\n{body}".strip()

        logger.info(f"Correo autorizado recibido de {sender_email}, creando tarea")
        self._create_task_from_email(prompt, sender_email)

    @staticmethod
    def _extract_email(from_header: str) -> str:
        """'Wilderman <wilderman@gmail.com>' -> 'wilderman@gmail.com'"""
        if "<" in from_header and ">" in from_header:
            return from_header.split("<")[1].split(">")[0].strip().lower()
        return from_header.strip().lower()

    def _get_message_body(self, message_id: str) -> str:
        full_message = self._service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = full_message.get("payload", {})
        return self._extract_plain_text(payload)

    def _extract_plain_text(self, payload: dict) -> str:
        """
        Los correos pueden venir como texto plano directo, o como
        multipart con varias versiones (texto plano + HTML). Preferimos
        siempre la parte text/plain — evita tener que parsear HTML.
        """
        mime_type = payload.get("mimeType", "")

        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""

        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""

        # Fallback: si no hay text/plain, intenta cualquier parte con datos
        for part in payload.get("parts", []):
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        return ""

    def _create_task_from_email(self, prompt: str, sender_email: str) -> None:
        try:
            response = requests.post(
                f"{self.api_base_url}/api/tasks",
                json={
                    "type": "DIRECTOR",
                    "prompt": prompt,
                    "metadata": {"sourceChannel": "gmail", "sourceAddress": sender_email},
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
        """
        Envía un correo. Se usa tanto para confirmar fallos al crear la
        tarea como para notificar resultados finales (ver main.py).
        """
        try:
            message = MIMEText(body)
            message["to"] = to_address
            message["subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

            self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError:
            logger.exception(f"No se pudo enviar correo a {to_address}")
