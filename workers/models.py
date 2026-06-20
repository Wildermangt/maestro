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
