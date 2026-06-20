"""
Punto de entrada del Worker Python.

Loop infinito: espera jobs en la cola BullMQ (via Redis), los enruta
al agente correspondiente según `type`, y publica el resultado.

El router usa inicialización perezosa (lazy): cada agente se instancia
solo la primera vez que se necesita, y solo una vez (se cachea). Esto
es deliberado — ResearcherAgent/AnalystAgent fallan en __init__ si
faltan las API keys requeridas, y no queremos que eso tumbe al worker
completo impidiendo que DirectorAgent (en su forma básica) siga
respondiendo.

El DirectorAgent (Sprint 3) necesita poder delegar a Researcher/Analyst
in-process — ver agents/director.py para el porqué. Para eso recibe
`AGENT_FACTORIES` completo, pero usando el mismo `get_agent()` perezoso
para que sus sub-agentes también se cacheen y compartan instancia con
el resto del worker (un solo ResearcherAgent, no uno por cada subtarea).
"""
import logging

from agents.analyst import AnalystAgent
from agents.director import DirectorAgent
from agents.researcher import ResearcherAgent
from bullmq_client import BullMQConsumer
from config import POLL_TIMEOUT_SECONDS, QUEUE_NAME, REDIS_URL
from models import SubTask, TaskJob, TaskResult, TaskStatus, TaskType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("worker")

AGENT_FACTORIES = {
    TaskType.DIRECTOR: DirectorAgent,
    TaskType.RESEARCH: ResearcherAgent,
    TaskType.ANALYSIS: AnalystAgent,
}

_agent_cache: dict = {}


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
            # cacheada de Researcher/Analyst en vez de crear una nueva
            # por cada subtarea.
            agent.set_agent_factories(
                {
                    TaskType.RESEARCH: lambda: get_agent(TaskType.RESEARCH),
                    TaskType.ANALYSIS: lambda: get_agent(TaskType.ANALYSIS),
                }
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
