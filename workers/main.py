"""
Punto de entrada del Worker Python.

Loop infinito: espera jobs en la cola BullMQ (via Redis), los enruta
al agente correspondiente según `type`, y publica el resultado.

El router usa inicialización perezosa (lazy): cada agente se instancia
solo la primera vez que se necesita, y solo una vez (se cachea). Esto
es deliberado — ResearcherAgent falla en __init__ si faltan las API
keys de Tavily/Firecrawl/Anthropic, y no queremos que eso tumbe al
worker completo impidiendo que DirectorAgent (que no necesita esas
keys) siga funcionando.
"""
import logging

from agents.director import DirectorAgent
from agents.researcher import ResearcherAgent
from bullmq_client import BullMQConsumer
from config import POLL_TIMEOUT_SECONDS, QUEUE_NAME, REDIS_URL
from models import TaskJob, TaskResult, TaskStatus, TaskType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")

# Factories en vez de instancias directas — se instancian on-demand.
AGENT_FACTORIES = {
    TaskType.DIRECTOR: DirectorAgent,
    TaskType.RESEARCH: ResearcherAgent,
}

_agent_cache: dict = {}


def get_agent(task_type: TaskType):
    if task_type not in AGENT_FACTORIES:
        logger.warning(f"No hay agente registrado para type={task_type}, usando Director como fallback")
        task_type = TaskType.DIRECTOR

    if task_type not in _agent_cache:
        _agent_cache[task_type] = AGENT_FACTORIES[task_type]()

    return _agent_cache[task_type]


def process_job(raw_payload: dict, consumer: BullMQConsumer) -> None:
    job_id = raw_payload.pop("_jobId", None)

    try:
        job = TaskJob.model_validate(raw_payload)
    except Exception as exc:
        logger.error(f"Payload inválido, no se pudo construir TaskJob: {exc}")
        return

    try:
        agent = get_agent(job.type)
    except Exception as exc:
        # Típicamente: falta una API key requerida por ese agente.
        logger.exception(f"No se pudo inicializar el agente para type={job.type}")
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
