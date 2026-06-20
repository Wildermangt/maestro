"""
Validadores Pydantic del contrato definido en shared/contracts.md.
El worker nunca confía ciegamente en el JSON que llega de Redis;
lo valida aquí antes de pasarlo a cualquier agente.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    DIRECTOR = "DIRECTOR"
    RESEARCH = "RESEARCH"
    ANALYSIS = "ANALYSIS"
    PRESENTATION = "PRESENTATION"
    WEBSITE = "WEBSITE"


class TaskStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class TaskJob(BaseModel):
    taskId: str
    type: TaskType
    prompt: str
    userId: str
    metadata: dict = Field(default_factory=dict)


class TaskResult(BaseModel):
    taskId: str
    status: TaskStatus
    result: dict
    artifacts: list = Field(default_factory=list)
    error: Optional[str] = None
    completedAt: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SourceRef(BaseModel):
    """Una fuente citada en la investigación, con su URL para verificación."""
    title: str
    url: str


class ResearchOutput(BaseModel):
    """
    Shape estructurado que el Agente Investigador produce.
    Validar esto contra la salida del LLM evita que un resumen mal
    formado o sin fuentes llegue al usuario como si fuera confiable.
    """
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)


class AnalysisOutput(BaseModel):
    """Shape estructurado que el Agente Analista produce."""
    summary: str
    metrics: dict = Field(default_factory=dict)
    insights: list[str] = Field(default_factory=list)


# --- Modelos del Agente Director (Sprint 3) ---

class SubTaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SubTask(BaseModel):
    """
    Una subtarea dentro del plan del Director. `dependsOn` lista los
    `id` de otras subtareas que deben completarse antes — eso es lo
    que el grafo usa para decidir qué puede correr en paralelo.
    """
    id: str
    agentType: TaskType
    prompt: str
    dependsOn: list[str] = Field(default_factory=list)
    status: SubTaskStatus = SubTaskStatus.PENDING
    result: Optional[dict] = None
    error: Optional[str] = None


class DirectorPlan(BaseModel):
    """Plan que el LLM genera al descomponer el objetivo del usuario."""
    subtasks: list[SubTask]


class SubTaskProgressEvent(BaseModel):
    """
    Evento que el Director publica cada vez que una subtarea cambia
    de estado. NestJS reenvía esto por WebSocket tal cual para que el
    frontend pinte el árbol de subtareas en tiempo real, sin esperar
    a que el Director termine todo el plan.
    """
    taskId: str  # taskId de la tarea DIRECTOR padre
    subtaskId: str
    agentType: TaskType
    status: SubTaskStatus
    prompt: str
    result: Optional[dict] = None
    error: Optional[str] = None
