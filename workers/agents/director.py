"""
Agente Director — versión Walking Skeleton.

En el diseño final (ver Sprint 3) este agente usa LangGraph para
descomponer objetivos en subtareas y delegar a agentes especializados.

Para el Sprint 1, su único trabajo es probar que el mensaje completo
el viaje: Frontend -> NestJS -> Redis -> Worker -> Redis -> NestJS -> WebSocket.
"""
import logging
import time

from agents.base import BaseAgent
from models import TaskJob, TaskResult, TaskStatus

logger = logging.getLogger("director")


class DirectorAgent(BaseAgent):
    name = "director"

    def run(self, job: TaskJob) -> TaskResult:
        logger.info(f"DirectorAgent procesando taskId={job.taskId} prompt='{job.prompt}'")

        # Simula trabajo real (en producción aquí iría el grafo LangGraph)
        time.sleep(1)

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result={
                "summary": "Hola Mundo",
                "data": {
                    "echo": job.prompt,
                    "receivedType": job.type.value,
                    "note": (
                        "Walking Skeleton operativo. El Agente Director real "
                        "con LangGraph llega en el Sprint 3."
                    ),
                },
            },
        )
