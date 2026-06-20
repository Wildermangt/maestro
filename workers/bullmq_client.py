"""
Cliente mínimo para consumir colas BullMQ desde Python.

BullMQ (Node.js) usa una estructura de claves Redis específica:
  bull:{queueName}:wait      -> lista de IDs de jobs pendientes
  bull:{queueName}:{jobId}   -> hash con los datos del job

No existe un cliente Python oficial maduro para BullMQ, así que aquí
hablamos directamente con las estructuras Redis que BullMQ define
internamente. Esto funciona para el Walking Skeleton (un solo worker,
sin prioridades, sin reintentos automáticos, sin rate limiting).

⚠️ LIMITACIÓN CONOCIDA — leer antes de escalar a producción:
Este cliente asume jobs simples agregados con `queue.add()` sin opciones
de prioridad. BullMQ real mueve jobs de 'wait' a 'active' mediante un
script Lua atómico que también gestiona reintentos, backoff, y --si se
usan prioridades-- un ZSET en lugar de una lista. Replicar manualmente
ese script Lua es frágil y se rompe silenciosamente si en el backend
NestJS alguien empieza a usar `priority`, `delay`, o `attempts` en las
opciones del job.

Camino recomendado para Sprint 2+: usar la librería oficial `bullmq`
de Python (https://github.com/taskforcesh/bullmq-python), que reimplementa
los mismos scripts Lua que el BullMQ de Node y mantiene compatibilidad
real entre ambos lenguajes. Migrar este archivo a ese cliente es el
primer ítem de deuda técnica a resolver después del Walking Skeleton.
"""
import json
import logging
import time
from typing import Optional

import redis

logger = logging.getLogger("bullmq_client")


class BullMQConsumer:
    def __init__(self, redis_url: str, queue_name: str):
        self.redis = redis.from_url(redis_url, decode_responses=True)
        self.queue_name = queue_name
        self.wait_key = f"bull:{queue_name}:wait"
        self.active_key = f"bull:{queue_name}:active"

    def fetch_job(self, timeout: int = 5) -> Optional[dict]:
        """
        Bloquea hasta `timeout` segundos esperando un job.
        Mueve el ID de 'wait' a 'active' (patrón estándar BullMQ)
        y devuelve los datos del job ya parseados.
        """
        result = self.redis.brpoplpush(self.wait_key, self.active_key, timeout=timeout)
        if result is None:
            return None

        job_id = result
        job_key = f"bull:{self.queue_name}:{job_id}"
        job_data = self.redis.hgetall(job_key)

        if not job_data:
            logger.warning(f"Job {job_id} encontrado en cola pero sin datos en {job_key}")
            return None

        try:
            payload = json.loads(job_data.get("data", "{}"))
        except json.JSONDecodeError:
            logger.error(f"Job {job_id} tiene payload JSON inválido")
            return None

        payload["_jobId"] = job_id
        return payload

    def mark_active_done(self, job_id: str):
        """Quita el job de la lista 'active' una vez procesado."""
        self.redis.lrem(self.active_key, 0, job_id)

    def publish_result(self, task_id: str, result: dict):
        """
        Escribe el resultado en Redis y publica en el canal pub/sub
        que NestJS escucha. Ver shared/contracts.md para el shape exacto.
        """
        result_key = f"result:{task_id}"
        self.redis.set(result_key, json.dumps(result), ex=3600)  # expira en 1h
        self.redis.publish("task-completed", json.dumps({"taskId": task_id}))
        logger.info(f"Resultado publicado para taskId={task_id}")

    def publish_progress(self, event: dict):
        """
        Publica un evento de progreso de subtarea (ver models.SubTaskProgressEvent)
        en un canal separado de 'task-completed'. A diferencia del resultado
        final, este evento no se persiste en una clave con TTL — es un
        stream efímero; si NestJS no está escuchando en el momento exacto,
        el evento se pierde (aceptable: el frontend siempre puede pedir el
        estado final consolidado vía GET /api/tasks/:id).
        """
        self.redis.publish("task-progress", json.dumps(event))
