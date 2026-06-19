"""
Punto de entrada del Worker Python.

Loop infinito: espera jobs en la cola BullMQ (via Redis), los enruta
al agente correspondiente según `type`, y publica el resultado.

Para el Walking Skeleton solo existe DirectorAgent, pero el router
ya está listo para añadir researcher/analyst/writer/etc. sin tocar
este archivo (Sprint 2+).
"""
import logging

from agents.director import DirectorAgent
from bullmq_client import BullMQConsumer
from config import POLL_TIMEOUT_SECONDS, QUEUE_NAME, REDIS_URL
from models import TaskJob, TaskResult, TaskStatus, TaskType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")

# Router de agentes por tipo de tarea.
# Sprint 2+: añadir RESEARCH -> ResearcherAgent(), ANALYSIS -> AnalystAgent(), etc.
AGENT_ROUTER = {
    TaskType.DIRECTOR: DirectorAgent(),
}


def process_job(raw_payload: dict, consumer: BullMQConsumer) -> None:
    job_id = raw_payload.pop("_jobId", None)

    try:
        job = TaskJob.model_validate(raw_payload)
    except Exception as exc:
        logger.error(f"Payload inválido, no se pudo construir TaskJob: {exc}")
        return

    agent = AGENT_ROUTER.get(job.type)
    if agent is None:
        logger.warning(f"No hay agente registrado para type={job.type}, usando Director como fallback")
        agent = AGENT_ROUTER[TaskType.DIRECTOR]

    try:
        result = agent.run(job)
    except Exception as exc:
        logger.exception(f"Agente {agent.name} falló procesando taskId={job.taskId}")
        result = TaskResult(
            taskId=job.taskId,
            status=TaskStatus.FAILED,
            result={},
            error=str(exc),
        )

    consumer.publish_result(job.taskId, result.model_dump())

    if job_id:
        consumer.mark_active_done(job_id)


def main() -> None:
    logger.info(f"Worker iniciado. Conectando a {REDIS_URL}, escuchando cola '{QUEUE_NAME}'")
    consumer = BullMQConsumer(redis_url=REDIS_URL, queue_name=QUEUE_NAME)

    while True:
        job_payload = consumer.fetch_job(timeout=POLL_TIMEOUT_SECONDS)
        if job_payload is None:
            continue  # timeout normal, vuelve a esperar

        logger.info(f"Job recibido: taskId={job_payload.get('taskId')}")
        process_job(job_payload, consumer)


if __name__ == "__main__":
    main()
