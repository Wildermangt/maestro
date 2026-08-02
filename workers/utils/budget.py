"""
Presupuesto de ejecución de una tarea.

Lleva la cuenta de lo que una tarea ya consumió —llamadas al LLM,
subtareas creadas, tiempo— y responde a una sola pregunta: ¿puedo
expandir más, o toca cerrar con lo que hay?

Diseño deliberado: **agotar el presupuesto no lanza excepción**. Los
métodos `puede_*` devuelven un booleano y el Director decide dejar de
expandir. Un límite alcanzado significa "entrega lo que lograste y
dilo", no "falla la tarea entera después de haber gastado la cuota".

La única excepción es `consumir_llamada_llm()`, que sí lanza
`PresupuestoAgotado` si se pasa: para cuando el tope se alcanza en
medio de un agente que ya empezó, y la alternativa sería seguir
gastando sin control.

Es thread-safe porque el Director ejecuta subtareas en paralelo con
ThreadPoolExecutor.
"""
import logging
import threading
import time

logger = logging.getLogger("utils.budget")


class PresupuestoAgotado(RuntimeError):
    """El tope de llamadas al LLM se alcanzó a mitad de ejecución."""


class TaskBudget:
    def __init__(
        self,
        max_llm_calls: int,
        max_subtasks: int,
        max_depth: int,
        max_verification_rounds: int,
        timeout_seconds: int,
    ):
        self.max_llm_calls = max_llm_calls
        self.max_subtasks = max_subtasks
        self.max_depth = max_depth
        self.max_verification_rounds = max_verification_rounds
        self.timeout_seconds = timeout_seconds

        self._llm_calls = 0
        self._subtasks = 0
        self._rondas = 0
        self._inicio = time.monotonic()
        self._lock = threading.Lock()
        self._motivos: list[str] = []

    # --- Consumo -----------------------------------------------------------

    def consumir_llamada_llm(self, cuantas: int = 1) -> None:
        with self._lock:
            if self._llm_calls + cuantas > self.max_llm_calls:
                self._anotar(f"tope de {self.max_llm_calls} llamadas al LLM alcanzado")
                raise PresupuestoAgotado(
                    f"Se alcanzó el tope de {self.max_llm_calls} llamadas al LLM para esta tarea. "
                    "Se entrega el resultado parcial. Ajusta MAX_LLM_CALLS_PER_TASK si necesitas más."
                )
            self._llm_calls += cuantas

    def registrar_subtareas(self, cuantas: int) -> None:
        with self._lock:
            self._subtasks += cuantas

    def registrar_ronda_verificacion(self) -> None:
        with self._lock:
            self._rondas += 1

    # --- Consultas ---------------------------------------------------------

    def cupo_de_subtareas(self) -> int:
        """Cuántas subtareas más caben. 0 significa no expandir más."""
        with self._lock:
            return max(0, self.max_subtasks - self._subtasks)

    def puede_profundizar(self, profundidad_actual: int) -> bool:
        if profundidad_actual + 1 > self.max_depth:
            self._anotar(f"profundidad máxima de descomposición ({self.max_depth}) alcanzada")
            return False
        return True

    def puede_verificar(self) -> bool:
        with self._lock:
            if self._rondas >= self.max_verification_rounds:
                return False
        return not self.tiempo_agotado()

    def tiempo_agotado(self) -> bool:
        agotado = (time.monotonic() - self._inicio) > self.timeout_seconds
        if agotado:
            self._anotar(f"tiempo máximo de tarea ({self.timeout_seconds}s) alcanzado")
        return agotado

    def llamadas_restantes(self) -> int:
        with self._lock:
            return max(0, self.max_llm_calls - self._llm_calls)

    # --- Reporte -----------------------------------------------------------

    def _anotar(self, motivo: str) -> None:
        if motivo not in self._motivos:
            self._motivos.append(motivo)
            logger.info(f"Presupuesto: {motivo}")

    @property
    def limitado(self) -> bool:
        return bool(self._motivos)

    def resumen(self) -> dict:
        with self._lock:
            return {
                "llamadasLLM": self._llm_calls,
                "maxLlamadasLLM": self.max_llm_calls,
                "subtareas": self._subtasks,
                "maxSubtareas": self.max_subtasks,
                "rondasVerificacion": self._rondas,
                "segundos": round(time.monotonic() - self._inicio, 1),
                "limitesAlcanzados": list(self._motivos),
            }


def presupuesto_por_defecto() -> TaskBudget:
    """Construye un presupuesto con los topes configurados en config.py."""
    from config import (
        MAX_DECOMPOSITION_DEPTH,
        MAX_LLM_CALLS_PER_TASK,
        MAX_SUBTASKS_PER_TASK,
        MAX_VERIFICATION_ROUNDS,
        TASK_TIMEOUT_SECONDS,
    )

    return TaskBudget(
        max_llm_calls=MAX_LLM_CALLS_PER_TASK,
        max_subtasks=MAX_SUBTASKS_PER_TASK,
        max_depth=MAX_DECOMPOSITION_DEPTH,
        max_verification_rounds=MAX_VERIFICATION_ROUNDS,
        timeout_seconds=TASK_TIMEOUT_SECONDS,
    )
