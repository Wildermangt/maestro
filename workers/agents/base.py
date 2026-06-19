"""
Agente base. En el Walking Skeleton solo el Director existe,
y lo único que hace es devolver "Hola Mundo" para probar el flujo.

A partir del Sprint 2, los agentes especializados (researcher.py,
analyst.py, etc.) heredarán de BaseAgent y sobreescribirán run().
"""
from abc import ABC, abstractmethod

from models import TaskJob, TaskResult


class BaseAgent(ABC):
    name: str = "base"

    @abstractmethod
    def run(self, job: TaskJob) -> TaskResult:
        ...
