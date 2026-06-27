"""
Integración con Telegram (Sprint 6).

Diseño: el bot de Telegram NO encola jobs directamente en Redis. En su
lugar, llama al mismo endpoint HTTP `POST /api/tasks` que usa el
frontend. Esto es deliberado: NestJS es la única fuente de verdad para
la creación de tareas (crea la fila en Postgres, encola en BullMQ,
maneja el contrato completo). Si Telegram encolara directo en Redis,
nunca existiría la fila Task en Postgres y el ResultListenerService
fallaría al intentar actualizarla cuando el worker termine.

Seguridad: el bot solo acepta mensajes de TELEGRAM_CHAT_ID (un único
chat fijo, definido en variables de entorno). Cualquier mensaje de
otro chat se ignora silenciosamente (no se responde, para no confirmar
a un desconocido que el bot existe y está activo).

Mecanismo: long polling (getUpdates con `offset`), no webhooks — no
requiere exponer el backend a internet con HTTPS, que es justo la
razón por la que se eligió este modo en vez de polling vs webhook.
"""
import logging
import os
import time

import requests

logger = logging.getLogger("channels.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_SECONDS = 30  # long polling: Telegram mantiene la conexión abierta


class TelegramChannel:
    def __init__(
        self,
        bot_token: str | None = None,
        allowed_chat_id: str | None = None,
        api_base_url: str | None = None,
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.allowed_chat_id = allowed_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        # URL interna del backend NestJS, para crear tareas vía su API
        # — NO la URL pública (no aplica dentro de la red de Docker).
        self.api_base_url = api_base_url or os.getenv("BACKEND_INTERNAL_URL", "http://backend:4000")

        if not self.bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN no está configurada. "
                "Define la variable de entorno antes de iniciar este canal."
            )
        if not self.allowed_chat_id:
            raise RuntimeError(
                "TELEGRAM_CHAT_ID no está configurada. "
                "Sin esto, el bot no sabría a quién responder ni a quién bloquear."
            )

        self._telegram_base = f"{TELEGRAM_API_BASE}/bot{self.bot_token}"
        self._last_update_id = 0

    def run_forever(self) -> None:
        """
        Loop de long-polling. Se ejecuta en un hilo separado del loop
        principal del worker (ver main.py) — son dos cosas independientes
        que comparten el mismo proceso pero no se bloquean entre sí.
        """
        logger.info("Canal de Telegram iniciado, escuchando mensajes...")
        while True:
            try:
                updates = self._get_updates()
                for update in updates:
                    self._handle_update(update)
            except requests.exceptions.RequestException:
                logger.exception("Error de red consultando Telegram, reintentando en 5s")
                time.sleep(5)
            except Exception:
                logger.exception("Error inesperado en el loop de Telegram, continuando")
                time.sleep(5)

    def _get_updates(self) -> list[dict]:
        response = requests.get(
            f"{self._telegram_base}/getUpdates",
            params={
                "offset": self._last_update_id + 1,
                "timeout": POLL_TIMEOUT_SECONDS,
            },
            timeout=POLL_TIMEOUT_SECONDS + 10,  # margen sobre el timeout de Telegram
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            logger.error(f"Telegram getUpdates devolvió error: {data}")
            return []

        results = data.get("result", [])
        if results:
            self._last_update_id = max(u["update_id"] for u in results)
        return results

    def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return  # otros tipos de update (ediciones, callbacks) se ignoran

        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        if chat_id != str(self.allowed_chat_id):
            logger.warning(f"Mensaje ignorado de chat_id no autorizado: {chat_id}")
            return  # silencioso a propósito — no confirmar que el bot existe

        if not text:
            self.send_message("Solo puedo procesar mensajes de texto por ahora.")
            return

        if text == "/start":
            self.send_message(
                "Prompt Maestro conectado. Escríbeme cualquier objetivo y lo "
                "envío al Director para que lo procese."
            )
            return

        self._create_task_from_message(text)

    def _create_task_from_message(self, prompt: str) -> None:
        try:
            response = requests.post(
                f"{self.api_base_url}/api/tasks",
                json={
                    "type": "DIRECTOR",
                    "prompt": prompt,
                    # Marca de origen: el worker la lee al completar el job
                    # (ver main.py) para saber que debe notificar por
                    # Telegram en vez de (o además de) WebSocket.
                    "metadata": {"sourceChannel": "telegram"},
                },
                timeout=10,
            )
            response.raise_for_status()
            task = response.json()
            self.send_message(
                f"✅ Tarea creada (id: {task['id'][:8]}...). "
                "Te aviso por aquí cuando el Director termine."
            )
        except requests.exceptions.RequestException as exc:
            logger.exception("Error creando tarea desde Telegram")
            self.send_message(
                f"⚠️ No pude crear la tarea. El backend respondió con un error: {exc}"
            )

    def send_message(self, text: str) -> None:
        """
        Envía un mensaje al chat autorizado. Se usa tanto para confirmar
        la creación de una tarea como para notificar resultados finales
        (ver workers/main.py, donde se llama tras completar un job que
        vino marcado como originado en Telegram).
        """
        try:
            response = requests.post(
                f"{self._telegram_base}/sendMessage",
                json={"chat_id": self.allowed_chat_id, "text": text},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("No se pudo enviar mensaje a Telegram")
