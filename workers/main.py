"""
Punto de entrada del Worker Python.

Loop infinito: espera jobs en la cola BullMQ (via Redis), los enruta
al agente correspondiente según `type`, y publica el resultado.

El router usa inicialización perezosa (lazy): cada agente se instancia
solo la primera vez que se necesita, y solo una vez (se cachea). Esto
es deliberado — ResearcherAgent/AnalystAgent/PresenterAgent/DesignerAgent
fallan en __init__ si faltan las API keys requeridas, y no queremos que
eso tumbe al worker completo impidiendo que DirectorAgent (en su forma
básica) siga respondiendo.

El DirectorAgent (Sprint 3+) necesita poder delegar a los demás agentes
in-process — ver agents/director.py para el porqué. Para eso recibe
`AGENT_FACTORIES` completo, pero usando el mismo `get_agent()` perezoso
para que sus sub-agentes también se cacheen y compartan instancia con
el resto del worker (una sola instancia de cada agente, no una por
cada subtarea).
"""
import logging
import threading

from agents.analyst import AnalystAgent
from agents.designer import DesignerAgent
from agents.director import DirectorAgent
from agents.presenter import PresenterAgent
from agents.researcher import ResearcherAgent
from bullmq_client import BullMQConsumer
from channels.gmail import GmailChannel
from channels.outlook import OutlookChannel
from channels.telegram import TelegramChannel
from config import POLL_TIMEOUT_SECONDS, QUEUE_NAME, REDIS_URL
from models import SubTask, TaskJob, TaskResult, TaskStatus, TaskType
from tracing import extract_context, get_tracer, setup_telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")

AGENT_FACTORIES = {
    TaskType.DIRECTOR: DirectorAgent,
    TaskType.RESEARCH: ResearcherAgent,
    TaskType.ANALYSIS: AnalystAgent,
    TaskType.PRESENTATION: PresenterAgent,
    TaskType.WEBSITE: DesignerAgent,
}

# Tipos que el Director puede delegar in-process. PRESENTATION y WEBSITE
# se agregan aquí (Sprint 4) además de RESEARCH/ANALYSIS (Sprint 3) —
# así un objetivo como "investiga X y crea una presentación" puede
# descomponerse en RESEARCH -> PRESENTATION con dependsOn.
DELEGABLE_TYPES = [TaskType.RESEARCH, TaskType.ANALYSIS, TaskType.PRESENTATION, TaskType.WEBSITE]

_agent_cache: dict = {}

# Canal de Telegram: opcional. Si TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no
# están configuradas, simplemente queda en None y el resto del worker
# sigue funcionando sin esa integración — mismo patrón de degradación
# que el resto de agentes con API keys opcionales.
try:
    _telegram_channel: TelegramChannel | None = TelegramChannel()
except RuntimeError as exc:
    logger.info(f"Canal de Telegram deshabilitado: {exc}")
    _telegram_channel = None

# Canales de email: mismo patrón de degradación opcional que Telegram.
try:
    _gmail_channel: GmailChannel | None = GmailChannel()
except RuntimeError as exc:
    logger.info(f"Canal de Gmail deshabilitado: {exc}")
    _gmail_channel = None

try:
    _outlook_channel: OutlookChannel | None = OutlookChannel()
except RuntimeError as exc:
    logger.info(f"Canal de Outlook deshabilitado: {exc}")
    _outlook_channel = None


def get_agent(task_type: TaskType):
    if task_type not in AGENT_FACTORIES:
        logger.warning(f"No hay agente registrado para type={task_type}, usando Director como fallback")
        task_type = TaskType.DIRECTOR

    if task_type not in _agent_cache:
        agent = AGENT_FACTORIES[task_type]()
        if task_type == TaskType.DIRECTOR:
            # El Director delega a estos tipos in-process. Le pasamos
            # factories (no instancias) que internamente usan este mismo
            # get_agent() perezoso, para reusar la misma instancia
            # cacheada en vez de crear una nueva por cada subtarea.
            agent.set_agent_factories(
                {t: (lambda t=t: get_agent(t)) for t in DELEGABLE_TYPES}
            )
        _agent_cache[task_type] = agent

    return _agent_cache[task_type]


def process_job(raw_payload: dict, consumer: BullMQConsumer) -> None:
    job_id = raw_payload.pop("_jobId", None)

    try:
        job = TaskJob.model_validate(raw_payload)
    except Exception as exc:
        logger.error(f"Payload inválido, no se pudo construir TaskJob: {exc}")
        return

    # Reconstruye el contexto de trace que NestJS propagó en el job (ver
    # shared/contracts.md, campo traceContext) para que este span quede
    # anidado bajo la traza de la petición HTTP original, no como una
    # traza nueva sin relación. Si el job no trae ese campo, simplemente
    # se abre una traza raíz nueva — extract_context() ya maneja eso.
    parent_context = extract_context(job.traceContext)
    tracer = get_tracer()

    with tracer.start_as_current_span(
        f"process_task.{job.type.value}",
        context=parent_context,
        attributes={
            "maestro.task_id": job.taskId,
            "maestro.task_type": job.type.value,
            "maestro.user_id": job.userId,
        },
    ) as span:
        _process_job_inner(job, job_id, consumer, span)


def _process_job_inner(job: TaskJob, job_id: str | None, consumer: BullMQConsumer, span) -> None:
    try:
        agent = get_agent(job.type)
    except Exception as exc:
        # Típicamente: falta una API key requerida por ese agente.
        logger.exception(f"No se pudo inicializar el agente para type={job.type}")
        span.set_attribute("maestro.status", "FAILED")
        span.record_exception(exc)
        result = TaskResult(
            taskId=job.taskId,
            status=TaskStatus.FAILED,
            result={},
            error=f"El agente no pudo inicializarse: {exc}",
        )
        consumer.publish_result(job.taskId, result.model_dump())
        if job_id:
            consumer.mark_active_done(job_id)
        return

    try:
        if job.type == TaskType.DIRECTOR:
            def on_progress(subtask: SubTask) -> None:
                try:
                    consumer.publish_progress(
                        {
                            "taskId": job.taskId,
                            "subtaskId": subtask.id,
                            "agentType": subtask.agentType.value,
                            "status": subtask.status.value,
                            "prompt": subtask.prompt,
                            "result": subtask.result,
                            "error": subtask.error,
                        }
                    )
                except Exception:
                    # Un fallo publicando progreso NUNCA debe tumbar la
                    # ejecución de la subtarea misma — es solo telemetría.
                    logger.exception(f"No se pudo publicar progreso de subtarea {subtask.id}")

            result = agent.run(job, on_progress=on_progress)
        else:
            result = agent.run(job)
    except Exception as exc:
        logger.exception(f"Agente {agent.name} falló procesando taskId={job.taskId}")
        span.set_attribute("maestro.status", "FAILED")
        span.record_exception(exc)
        result = TaskResult(
            taskId=job.taskId,
            status=TaskStatus.FAILED,
            result={},
            error=str(exc),
        )

    span.set_attribute("maestro.status", result.status.value)
    consumer.publish_result(job.taskId, result.model_dump())

    _notify_source_channel(job, result)

    if job_id:
        consumer.mark_active_done(job_id)


def _notify_source_channel(job: TaskJob, result: TaskResult) -> None:
    """
    Si la tarea se originó en un canal de mensajería o email (Telegram,
    Gmail, Outlook), le envía el resultado de vuelta por ese mismo canal.
    La notificación por WebSocket (vía NestJS) ocurre siempre y de forma
    independiente — esto es un AVISO adicional, no reemplaza el flujo normal.
    """
    source = job.metadata.get("sourceChannel")
    if source is None:
        return

    summary = result.result.get("summary") if isinstance(result.result, dict) else None
    if result.status == TaskStatus.FAILED:
        text = f"⚠️ La tarea falló: {result.error or 'sin detalle'}"
    elif summary:
        text = f"✅ Listo:\n\n{summary}"
    else:
        text = f"✅ Tarea completada (status={result.status.value})."

    try:
        if source == "telegram" and _telegram_channel is not None:
            _telegram_channel.send_message(text)
        elif source == "gmail" and _gmail_channel is not None:
            sender_address = job.metadata.get("sourceAddress")
            if sender_address:
                _gmail_channel.send_message(sender_address, "Resultado de tu tarea — Prompt Maestro", text)
        elif source == "outlook" and _outlook_channel is not None:
            sender_address = job.metadata.get("sourceAddress")
            if sender_address:
                _outlook_channel.send_message(sender_address, "Resultado de tu tarea — Prompt Maestro", text)
    except Exception:
        logger.exception(f"No se pudo notificar el resultado por canal '{source}'")


def main() -> None:
    setup_telemetry()
    logger.info(f"Worker iniciado. Conectando a {REDIS_URL}, escuchando cola '{QUEUE_NAME}'")
    consumer = BullMQConsumer(redis_url=REDIS_URL, queue_name=QUEUE_NAME)

    if _telegram_channel is not None:
        # Hilo independiente: el long-polling de Telegram bloquea por hasta
        # 30s en cada llamada (ver TELEGRAM_POLL_TIMEOUT_SECONDS), así que
        # correrlo en el mismo hilo que el loop principal pausaría el
        # procesamiento de jobs de BullMQ durante esas esperas. daemon=True
        # para que este hilo no impida que el proceso termine si el loop
        # principal se detiene.
        telegram_thread = threading.Thread(
            target=_telegram_channel.run_forever, daemon=True, name="telegram-channel"
        )
        telegram_thread.start()

    if _gmail_channel is not None:
        gmail_thread = threading.Thread(
            target=_gmail_channel.run_forever, daemon=True, name="gmail-channel"
        )
        gmail_thread.start()

    if _outlook_channel is not None:
        outlook_thread = threading.Thread(
            target=_outlook_channel.run_forever, daemon=True, name="outlook-channel"
        )
        outlook_thread.start()

    while True:
        job_payload = consumer.fetch_job(timeout=POLL_TIMEOUT_SECONDS)
        if job_payload is None:
            continue  # timeout normal, vuelve a esperar

        logger.info(f"Job recibido: taskId={job_payload.get('taskId')}")
        process_job(job_payload, consumer)


if __name__ == "__main__":
    main()
