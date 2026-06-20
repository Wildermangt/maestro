"""
Agente Director — orquestador con LangGraph (Sprint 3).

Flujo del grafo:
  decompose -> execute -> consolidate -> END

1. decompose: Claude descompone el objetivo del usuario en un
   DirectorPlan (lista de SubTask con dependsOn).
2. execute: ejecuta las subtareas respetando dependencias. Las que
   no dependen entre sí corren en paralelo (ThreadPoolExecutor, ya
   que los agentes hacen I/O bloqueante: HTTP a Tavily/Firecrawl/Claude).
   Publica un SubTaskProgressEvent por cada cambio de estado, ANTES
   y DESPUÉS de ejecutar cada subtarea, para que el frontend pueda
   pintar el árbol en tiempo real sin esperar al final.
3. consolidate: Claude redacta un resumen ejecutivo final a partir
   de los resultados de todas las subtareas.

Los agentes delegados (Investigador, Analista) se invocan IN-PROCESS
—llamando directamente a su método .run()— en vez de volver a encolar
en BullMQ. Encolar de vuelta introduciría un round-trip por Redis y la
posibilidad de que el propio worker se quede esperando su propio job
(deadlock si solo hay un worker). Invocar in-process es más simple y
correcto para este diseño de un solo proceso worker.

Limitación conocida: si en producción se escalan múltiples workers,
ejecutar subtareas in-process significa que todo el plan corre en el
worker que tomó el job DIRECTOR — no se reparte entre workers. Repartir
subtareas entre workers reales requeriría volver a encolarlas con un
mecanismo que evite el deadlock (ej: una cola separada por tipo de
agente). Lo dejamos así para el Sprint 3 y lo revisamos en Sprint 5
(Producción y Escalado) si el volumen lo justifica.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, TypedDict

from langgraph.graph import StateGraph, END

from agents.base import BaseAgent
from models import (
    DirectorPlan,
    SubTask,
    SubTaskStatus,
    TaskJob,
    TaskResult,
    TaskStatus,
    TaskType,
)
from utils.llm_factory import get_llm_client

logger = logging.getLogger("director")

DECOMPOSE_SYSTEM_PROMPT = """Eres un Director de Proyectos experto en \
descomponer objetivos complejos en tareas ejecutables por agentes \
especializados. Tienes disponibles estos tipos de agente:

- RESEARCH: investiga un tema en la web (búsqueda + lectura de fuentes + síntesis).
- ANALYSIS: analiza datos/calcula métricas a partir de un objetivo (genera y \
ejecuta código Python). Si una subtarea ANALYSIS depende de los hallazgos \
de una subtarea RESEARCH, indícalo en dependsOn.

Analiza el objetivo del usuario y genera un plan de ejecución con entre \
1 y 5 subtareas. Considera paralelismo: solo declares dependsOn cuando \
una subtarea REALMENTE necesite el resultado de otra (ej: analizar datos \
que la investigación todavía no ha encontrado). Si dos subtareas son \
independientes, no las enlaces — así pueden correr en paralelo.

Responde ÚNICAMENTE con un JSON con este shape exacto, sin texto adicional, \
sin backticks de markdown:

{
  "subtasks": [
    {"id": "string corto único, ej 't1'", "agentType": "RESEARCH" o "ANALYSIS", \
"prompt": "instrucción específica y autocontenida para ese agente", "dependsOn": ["t1", ...]}
  ]
}"""

CONSOLIDATE_SYSTEM_PROMPT = """Eres un Director de Proyectos. Recibiste un \
objetivo del usuario y ya se ejecutaron varias subtareas especializadas. \
Redacta un resumen ejecutivo claro (3-6 párrafos) que integre los \
resultados de todas las subtareas en una respuesta coherente para el \
usuario final. Si alguna subtarea falló, menciónalo brevemente sin \
detenerte en eso — prioriza lo que sí se logró.

Responde en texto plano, sin JSON, sin backticks."""


class DirectorState(TypedDict):
    job: TaskJob
    plan: DirectorPlan
    on_progress: Callable[[SubTask], None]
    final_summary: str


class DirectorAgent(BaseAgent):
    name = "director"

    def __init__(self, agent_factories: dict[TaskType, Callable[[], BaseAgent]] | None = None):
        self.llm = get_llm_client(provider="claude")
        # Inyectado por main.py para evitar import circular (director
        # delega a researcher/analyst, que viven en el mismo paquete agents).
        self._agent_factories = agent_factories or {}
        self._sub_agent_cache: dict[TaskType, BaseAgent] = {}
        self._graph = self._build_graph()

    def set_agent_factories(self, factories: dict[TaskType, Callable[[], BaseAgent]]):
        self._agent_factories = factories

    def run(self, job: TaskJob, on_progress: Callable[[SubTask], None] | None = None) -> TaskResult:
        logger.info(f"DirectorAgent procesando taskId={job.taskId} prompt='{job.prompt}'")

        initial_state: DirectorState = {
            "job": job,
            "plan": DirectorPlan(subtasks=[]),
            "on_progress": on_progress or (lambda _subtask: None),
            "final_summary": "",
        }

        try:
            final_state = self._graph.invoke(initial_state)
        except Exception as exc:
            logger.exception(f"El grafo del Director falló para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"El Director falló: {exc}",
            )

        plan: DirectorPlan = final_state["plan"]
        any_failed = any(st.status == SubTaskStatus.FAILED for st in plan.subtasks)
        all_failed = all(st.status == SubTaskStatus.FAILED for st in plan.subtasks)

        status = (
            TaskStatus.FAILED if all_failed
            else TaskStatus.PARTIAL if any_failed
            else TaskStatus.COMPLETED
        )

        return TaskResult(
            taskId=job.taskId,
            status=status,
            result={
                "summary": final_state["final_summary"],
                "subtasks": [st.model_dump() for st in plan.subtasks],
            },
        )

    # --- Construcción del grafo ---

    def _build_graph(self):
        workflow = StateGraph(DirectorState)
        workflow.add_node("decompose", self._decompose_node)
        workflow.add_node("execute", self._execute_node)
        workflow.add_node("consolidate", self._consolidate_node)

        workflow.set_entry_point("decompose")
        workflow.add_edge("decompose", "execute")
        workflow.add_edge("execute", "consolidate")
        workflow.add_edge("consolidate", END)

        return workflow.compile()

    # --- Nodos ---

    def _decompose_node(self, state: DirectorState) -> DirectorState:
        job = state["job"]
        raw = self.llm.complete(
            system=DECOMPOSE_SYSTEM_PROMPT,
            user=f"OBJETIVO DEL USUARIO: {job.prompt}",
            max_tokens=1500,
        )
        cleaned = self._strip_markdown_fence(raw)

        try:
            parsed = json.loads(cleaned)
            plan = DirectorPlan.model_validate(parsed)
        except Exception:
            logger.exception("El LLM no devolvió un plan válido, usando fallback de una sola subtarea RESEARCH")
            plan = DirectorPlan(
                subtasks=[SubTask(id="t1", agentType=TaskType.RESEARCH, prompt=job.prompt)]
            )

        logger.info(f"Plan generado: {len(plan.subtasks)} subtarea(s)")
        return {**state, "plan": plan}

    def _execute_node(self, state: DirectorState) -> DirectorState:
        plan = state["plan"]
        job = state["job"]
        on_progress = state["on_progress"]

        subtasks_by_id = {st.id: st for st in plan.subtasks}
        completed_ids: set[str] = set()

        def is_ready(st: SubTask) -> bool:
            return st.status == SubTaskStatus.PENDING and all(dep in completed_ids for dep in st.dependsOn)

        # Bucle de oleadas: en cada oleada se ejecutan en paralelo todas
        # las subtareas cuyas dependencias ya completaron. Esto respeta
        # el orden parcial del grafo sin necesitar un scheduler complejo.
        while True:
            ready = [st for st in plan.subtasks if is_ready(st)]
            if not ready:
                break

            with ThreadPoolExecutor(max_workers=max(len(ready), 1)) as pool:
                futures = {
                    pool.submit(self._run_subtask, st, job, subtasks_by_id, on_progress): st
                    for st in ready
                }
                for future in as_completed(futures):
                    st = futures[future]
                    try:
                        future.result()
                    except Exception:
                        logger.exception(f"Subtarea {st.id} lanzó una excepción no controlada")
                    completed_ids.add(st.id)

        # Cualquier subtarea que nunca quedó "ready" (dependencia rota,
        # ciclo en el plan generado por el LLM) se marca FAILED explícitamente
        # en vez de quedarse silenciosamente en PENDING para siempre.
        for st in plan.subtasks:
            if st.status == SubTaskStatus.PENDING:
                st.status = SubTaskStatus.FAILED
                st.error = "Dependencias no resueltas (posible ciclo en el plan generado)"
                on_progress(st)

        return {**state, "plan": plan}

    def _run_subtask(
        self,
        subtask: SubTask,
        parent_job: TaskJob,
        subtasks_by_id: dict[str, SubTask],
        on_progress: Callable[[SubTask], None],
    ) -> None:
        subtask.status = SubTaskStatus.RUNNING
        on_progress(subtask)

        try:
            agent = self._get_sub_agent(subtask.agentType)

            # Si esta subtarea depende de otra, le pasamos su resultado
            # como contexto (ver agents/analyst.py: lee metadata.context_data).
            context_data = None
            if subtask.dependsOn:
                dep = subtasks_by_id.get(subtask.dependsOn[0])
                if dep and dep.result:
                    context_data = dep.result

            sub_job = TaskJob(
                taskId=f"{parent_job.taskId}:{subtask.id}",
                type=subtask.agentType,
                prompt=subtask.prompt,
                userId=parent_job.userId,
                metadata={"context_data": context_data} if context_data else {},
            )

            sub_result = agent.run(sub_job)

            if sub_result.status == TaskStatus.FAILED:
                subtask.status = SubTaskStatus.FAILED
                subtask.error = sub_result.error
            else:
                subtask.status = SubTaskStatus.COMPLETED
                subtask.result = sub_result.result

        except Exception as exc:
            logger.exception(f"Subtarea {subtask.id} (agentType={subtask.agentType}) falló")
            subtask.status = SubTaskStatus.FAILED
            subtask.error = str(exc)

        on_progress(subtask)

    def _consolidate_node(self, state: DirectorState) -> DirectorState:
        job = state["job"]
        plan = state["plan"]

        subtasks_summary = "\n\n".join(
            f"SUBTAREA [{st.agentType.value}] (status={st.status.value}): {st.prompt}\n"
            f"Resultado: {json.dumps(st.result, ensure_ascii=False) if st.result else 'N/A'}\n"
            f"Error: {st.error or 'N/A'}"
            for st in plan.subtasks
        )

        try:
            summary = self.llm.complete(
                system=CONSOLIDATE_SYSTEM_PROMPT,
                user=f"OBJETIVO ORIGINAL: {job.prompt}\n\nSUBTAREAS EJECUTADAS:\n\n{subtasks_summary}",
                max_tokens=1500,
            )
        except Exception:
            logger.exception("La consolidación final con el LLM falló, usando resumen genérico")
            completed = sum(1 for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED)
            summary = (
                f"Se ejecutaron {len(plan.subtasks)} subtareas, {completed} completadas "
                "exitosamente. No fue posible generar el resumen ejecutivo automático; "
                "revisa el detalle de cada subtarea abajo."
            )

        return {**state, "final_summary": summary.strip()}

    def _get_sub_agent(self, agent_type: TaskType) -> BaseAgent:
        if agent_type not in self._sub_agent_cache:
            factory = self._agent_factories.get(agent_type)
            if factory is None:
                raise RuntimeError(f"No hay agente registrado para delegar type={agent_type}")
            self._sub_agent_cache[agent_type] = factory()
        return self._sub_agent_cache[agent_type]

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:] if lines[0].startswith("```") else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)
        return cleaned
