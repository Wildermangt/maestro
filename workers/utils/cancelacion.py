"""
Señal de cancelación de tareas.

NestJS marca una clave en Redis cuando el usuario cancela; el Director la
consulta entre oleadas de subtareas y se detiene de forma ordenada,
conservando lo ya completado.

Por qué Redis y no la base de datos: el worker ya está conectado a Redis
para la cola, y una consulta por oleada debe ser barata. Consultar
Postgres desde el worker además rompería la regla de que NestJS es la
única fuente de verdad sobre la base.

La cancelación es COOPERATIVA: no mata el proceso. Una subtarea que ya
empezó termina —no tiene sentido tirar a la basura una llamada al LLM que
ya se pagó— y lo que se corta es el arranque de las siguientes.
"""
import logging
import os

import redis

logger = logging.getLogger("utils.cancelacion")

_TTL_SEGUNDOS = 3600  # la señal se limpia sola; una tarea no dura tanto
_cliente = None


def _redis():
    global _cliente
    if _cliente is None:
        _cliente = redis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379"), decode_responses=True
        )
    return _cliente


def _clave(task_id: str) -> str:
    return f"cancel:{task_id}"


def marcar_cancelada(task_id: str) -> None:
    try:
        _redis().set(_clave(task_id), "1", ex=_TTL_SEGUNDOS)
        logger.info(f"Tarea {task_id} marcada para cancelación")
    except Exception:
        logger.exception(f"No se pudo marcar la cancelación de {task_id}")


def esta_cancelada(task_id: str) -> bool:
    """Nunca lanza: si Redis no responde, se asume que no hay cancelación."""
    try:
        return _redis().exists(_clave(task_id)) == 1
    except Exception:
        logger.warning("No se pudo consultar la señal de cancelación; se continúa")
        return False


def limpiar(task_id: str) -> None:
    try:
        _redis().delete(_clave(task_id))
    except Exception:
        pass
