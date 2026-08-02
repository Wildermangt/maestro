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
    # Agentes añadidos para que el sistema aguante objetivos grandes:
    EXTRACTION = "EXTRACTION"      # fuentes -> filas estructuradas
    ENRICHMENT = "ENRICHMENT"      # visita cada sitio y completa contactos
    VERIFICATION = "VERIFICATION"  # ¿el resultado cumple lo que se pidió?
    DOCUMENT = "DOCUMENT"          # informe en Word
    SPREADSHEET = "SPREADSHEET"    # libro Excel con varias hojas
    INGEST = "INGEST"              # leer archivos aportados por el usuario


class TaskStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    # El Director descompuso el objetivo y espera que el usuario apruebe
    # el plan antes de ejecutar nada. Ninguna subtarea se ha corrido aún,
    # así que todavía no se ha gastado presupuesto en ellas.
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    # La tarea se detuvo por petición del usuario. Lo ya completado se
    # conserva y se puede reanudar.
    CANCELLED = "CANCELLED"


class TaskJob(BaseModel):
    taskId: str
    type: TaskType
    prompt: str
    userId: str
    metadata: dict = Field(default_factory=dict)
    # Contexto de trace W3C inyectado por NestJS (ver shared/contracts.md).
    # Opcional: si el job se encoló sin esto (ej. pruebas manuales), el
    # worker simplemente abre un span raíz nuevo en vez de fallar.
    traceContext: Optional[dict] = None


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
    # Cuando el objetivo pide una tabla o un archivo descargable, el
    # script generado devuelve el CSV COMO TEXTO aquí, y el agente lo
    # escribe en disco. Se hace así, y no dejando que el script escriba
    # el archivo, para que el sandbox siga sellado: en modo Docker no
    # tiene montado el volumen de artefactos ni filesystem escribible.
    csv_content: Optional[str] = None
    csv_filename: Optional[str] = None


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
    artifacts: list[dict] = Field(default_factory=list)
    # Marca las subtareas que añadió una ronda de replanificación, para
    # distinguirlas del plan original al mostrar el árbol al usuario.
    ronda: int = 0


# --- Agente Extractor ---

class ExtractionOutput(BaseModel):
    """
    Filas estructuradas extraídas de fuentes. Es la pieza que faltaba
    entre el Investigador (que devuelve prosa) y el Analista (que
    necesita datos tabulares): sin esto, el Analista tenía que
    re-deducir las filas a partir de un resumen narrativo.
    """
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    summary: str = ""
    # Cuántas filas pedía el objetivo, si lo decía. Permite al
    # Verificador comparar contra lo realmente extraído.
    expected_count: Optional[int] = None


# --- Agente Verificador ---

class VerificationGap(BaseModel):
    """Algo que falta o está mal en el resultado, y cómo resolverlo."""
    descripcion: str
    # Instrucción autocontenida para una subtarea nueva que lo resuelva.
    # None si el hueco no es subsanable automáticamente.
    accion_sugerida: Optional[str] = None
    agente_sugerido: Optional[TaskType] = None


class VerificationOutput(BaseModel):
    """Veredicto del Verificador sobre si se cumplió el objetivo."""
    cumple: bool
    puntaje: float = 0.0  # 0.0 a 1.0
    resumen: str = ""
    huecos: list[VerificationGap] = Field(default_factory=list)


# --- Agentes de documento y hoja de cálculo ---

class DocumentOutput(BaseModel):
    summary: str
    section_count: int = 0


class SheetSpec(BaseModel):
    """Una hoja del libro Excel."""
    name: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)


class SpreadsheetPlan(BaseModel):
    filename: str = "datos.xlsx"
    sheets: list[SheetSpec] = Field(default_factory=list)


class SpreadsheetOutput(BaseModel):
    summary: str
    sheet_count: int = 0
    total_rows: int = 0


# --- Agente de ingesta ---

class IngestOutput(BaseModel):
    summary: str
    files_read: list[str] = Field(default_factory=list)
    content: str = ""


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


# --- Modelos del Agente Presentador (Sprint 4) ---

class SlideContent(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)


class PresentationPlan(BaseModel):
    """Estructura de slides que Claude genera antes de construir el PPTX."""
    deck_title: str
    slides: list[SlideContent]


class PresentationOutput(BaseModel):
    """Resultado final que el Presentador devuelve al Director/usuario."""
    summary: str
    slide_count: int
    artifactId: str  # referencia al Artifact creado en Postgres (ver backend)


# --- Modelos del Agente Diseñador Web (Sprint 4) ---

class WebsiteOutput(BaseModel):
    """Resultado final que el Diseñador Web devuelve."""
    summary: str
    deployUrl: Optional[str] = None  # None si el deploy a Vercel no se pudo hacer
    deployed: bool = False
