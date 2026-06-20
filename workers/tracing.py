"""
Configuración de OpenTelemetry para el worker Python.

Exportador actual: consola (stdout), misma decisión que el backend
NestJS (ver backend/src/telemetry/tracing.ts) — no hay backend de
observabilidad externo todavía en este sprint. Cambiar a un colector
OTLP real más adelante es solo cambiar el exportador, no la
instrumentación de abajo.

Este módulo expone dos helpers:
  - get_tracer(): para que main.py y los agentes generen sus propios spans.
  - extract_context(traceContext): para reconstruir el contexto de trace
    que NestJS propagó dentro del payload del job (ver shared/contracts.md,
    campo `traceContext`), de forma que los spans del worker queden como
    HIJOS del span de la petición HTTP original, no como una traza nueva
    sin relación. Si el job no trae traceContext (ej. uno encolado
    manualmente para pruebas), se inicia un span raíz nuevo sin fallar.
"""
import logging

from opentelemetry import trace, context as otel_context
from opentelemetry.propagate import extract
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = logging.getLogger("telemetry")

_initialized = False


def setup_telemetry() -> None:
    global _initialized
    if _initialized:
        return

    resource = Resource.create({"service.name": "maestro-worker"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)

    _initialized = True
    logger.info("OpenTelemetry inicializado (exportador: consola)")


def get_tracer():
    return trace.get_tracer("maestro-worker")


def extract_context(trace_carrier: dict | None):
    """
    Reconstruye el contexto de trace a partir del carrier W3C que NestJS
    inyectó en el job (campo `traceContext`). Devuelve un Context que se
    usa como `context=` al abrir un span, para que quede anidado bajo el
    span de la petición HTTP original en vez de empezar una traza nueva.

    Si trace_carrier es None o vacío (job sin ese campo), devuelve el
    contexto vacío actual — el span resultante simplemente será una
    traza raíz nueva, sin error.
    """
    if not trace_carrier:
        return otel_context.get_current()
    return extract(trace_carrier)
