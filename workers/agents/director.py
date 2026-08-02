"""
Agente Director — orquestador con LangGraph.

Flujo del grafo:
  decompose -> execute -> verify -> (replan -> execute)* -> consolidate

1. decompose: el LLM descompone el objetivo en un DirectorPlan. Para
   objetivos grandes, se le instruye trocear por lotes en vez de crear
   una sola subtarea gigante (una búsqueda enorme no cabe en el límite
   de la API de Tavily y devuelve resultados genéricos).
2. execute: ejecuta las subtareas en oleadas, en paralelo cuando no
   dependen entre sí. Una subtarea recibe el resultado de TODAS sus
   dependencias, no solo la primera.
3. verify: el Verificador contrasta lo obtenido contra el objetivo.
4. replan: si faltan cosas y queda presupuesto, convierte cada hueco en
   una subtarea nueva y vuelve a execute. Máximo MAX_VERIFICATION_ROUNDS.
5. consolidate: resumen ejecutivo final.

Presupuesto: toda la expansión (subtareas, profundidad, rondas, tiempo)
está acotada por un TaskBudget. Al alcanzar un tope no se falla: se deja
de expandir y se consolida lo logrado. Ver utils/budget.py.

Descomposición recursiva: una subtarea puede ser de tipo DIRECTOR, con
la profundidad acotada por MAX_DECOMPOSITION_DEPTH. Así un objetivo
grande se parte en bloques y cada bloque en pasos concretos.

Los agentes delegados se invocan IN-PROCESS —llamando a su .run()— en
vez de volver a encolar en BullMQ: encolar de vuelta abriría la puerta a
que el worker espere su propio job (deadlock con un solo worker).

Limitación conocida: si se escalan varios workers, todo el plan sigue
corriendo en el worker que tomó el job DIRECTOR. Repartirlo requeriría
una cola por tipo de agente.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, TypedDict

from langgraph.graph import StateGraph, END

from agents.base import BaseAgent
from config import MAX_SUBTASKS_PER_PLAN, REQUIRE_PLAN_APPROVAL
from memory.entity_store import resumen_para_contexto
from utils.cancelacion import esta_cancelada, limpiar
from models import (
    DirectorPlan,
    SubTask,
    SubTaskStatus,
    TaskJob,
    TaskResult,
    TaskStatus,
    TaskType,
)
from utils.budget import PresupuestoAgotado, TaskBudget, presupuesto_por_defecto
from utils.llm_factory import get_llm_client

logger = logging.getLogger("director")

# Costo aproximado en llamadas al LLM de ejecutar una subtarea de cada
# tipo. Los sub-agentes construyen su propio cliente y no conocen el
# presupuesto de la tarea, así que el Director carga aquí una ESTIMACIÓN
# en vez de un conteo exacto. Es suficiente para el propósito —evitar que
# un objetivo grande agote la cuota diaria— y no exige reescribir cómo
# los agentes obtienen su cliente.
COSTO_ESTIMADO: dict[TaskType, int] = {
    TaskType.RESEARCH: 2,       # condensar consulta + sintetizar
    TaskType.ANALYSIS: 2,       # generar código + posible reintento
    TaskType.EXTRACTION: 1,
    TaskType.ENRICHMENT: 1,   # 1 llamada al LLM + N scrapes de Firecrawl
    TaskType.PRESENTATION: 1,
    TaskType.WEBSITE: 1,
    TaskType.DOCUMENT: 1,
    TaskType.SPREADSHEET: 1,    # 0 si reutiliza una tabla ya estructurada
    TaskType.VERIFICATION: 1,
    TaskType.INGEST: 0,         # no usa LLM
    TaskType.DIRECTOR: 4,       # su propio decompose + verify + consolidate
}

DECOMPOSE_SYSTEM_PROMPT = """Eres un Director de Proyectos experto en \
descomponer objetivos complejos en tareas ejecutables por agentes \
especializados. Tienes disponibles estos tipos de agente:

- RESEARCH: investiga un tema en la web (búsqueda + lectura de fuentes + \
síntesis). Devuelve prosa y fuentes, no tablas.
- EXTRACTION: convierte el material de una investigación previa en filas \
estructuradas (tabla). SIEMPRE debe depender de una subtarea RESEARCH o \
INGEST. Úsalo cuando el objetivo pida datos de entidades: empresas, \
contactos, productos, precios.
- ENRICHMENT: recibe una tabla de EXTRACTION y VISITA el sitio web de \
cada fila para completar teléfono, correo y dirección. Es el único agente \
que entra a sitios concretos; RESEARCH solo sabe buscar en la web y \
devuelve datos genéricos si le pides los contactos de una lista. Úsalo \
SIEMPRE que el objetivo pida datos de contacto de empresas. Debe depender \
de la subtarea EXTRACTION.
- ANALYSIS: calcula métricas y genera archivos CSV (escribe y ejecuta \
código Python).
- SPREADSHEET: genera un libro de Excel (.xlsx) con varias hojas.
- DOCUMENT: genera un informe en Word (.docx).
- PRESENTATION: genera una presentación PPTX.
- WEBSITE: genera y despliega un sitio web estático de una página.
- INGEST: lee archivos que el usuario ya aportó. Solo si el objetivo los \
menciona explícitamente.
- DIRECTOR: descompone a su vez un bloque que sigue siendo demasiado \
grande para un solo agente. Úsalo con moderación: cada nivel multiplica \
el costo.

REGLAS DE DISEÑO DEL PLAN

1. Cadena para datos de entidades. Si el objetivo pide un listado de \
empresas, contactos, productos o precios, el plan correcto es: \
RESEARCH (buscar) -> EXTRACTION (estructurar, dependsOn la anterior) -> \
y si se pidió archivo, ANALYSIS o SPREADSHEET (dependsOn la extracción). \
No saltes la extracción: sin ella el archivo sale con datos inventados.

Si además se piden DATOS DE CONTACTO (teléfono, correo, dirección), \
intercala ENRICHMENT entre la extracción y el archivo: \
RESEARCH -> EXTRACTION -> ENRICHMENT -> SPREADSHEET. Nunca uses RESEARCH \
para buscar los contactos de una lista de empresas ya identificadas: no \
sabe entrar a sus sitios y devuelve información genérica inservible.

2. Trocea por lotes, no por cantidad. Si el objetivo pide muchos \
elementos ("los 50 proveedores"), NO crees una sola subtarea RESEARCH \
gigante: divide la búsqueda en 3 a 5 subtareas por SEGMENTO temático o \
geográfico (por ejemplo: cableado estructurado, seguridad electrónica, \
cómputo y servidores, redes y conectividad). Cada una busca mejor y da \
resultados más específicos. Después una sola EXTRACTION que dependa de \
todas ellas.

3. Archivos. Si el usuario pide CSV/Excel/Word/presentación/sitio web, \
el plan DEBE terminar con la subtarea que genera ese archivo. No \
devuelvas solo la investigación.

4. Paralelismo. Solo declara dependsOn cuando una subtarea NECESITE el \
resultado de otra. Las independientes corren en paralelo.

5. Cada prompt de subtarea debe ser autocontenido: quien lo ejecute no \
ve el objetivo original.

6. USA LA MEMORIA. Si el mensaje incluye una sección MEMORIA con empresas \
ya conocidas, NO las vuelvas a buscar. Si el objetivo pide más elementos, \
planifica búsquedas de otros segmentos o subsectores para encontrar \
DISTINTOS. Si a las conocidas les faltan datos de contacto, planifica \
directamente un ENRICHMENT sobre ellas y sáltate la investigación: los \
datos ya están, solo hay que completarlos.

Genera entre 1 y {max_subtareas} subtareas. Si el objetivo es simple, \
una sola está bien: no infles el plan.

Responde ÚNICAMENTE con un JSON con este shape exacto, sin texto \
adicional, sin backticks de markdown:

{{
  "subtasks": [
    {{"id": "t1", "agentType": "RESEARCH", "prompt": "instrucción autocontenida", "dependsOn": []}}
  ]
}}"""

CONSOLIDATE_SYSTEM_PROMPT = """Eres un Director de Proyectos. Recibiste un \
objetivo del usuario y ya se ejecutaron varias subtareas especializadas. \
Redacta un resumen ejecutivo claro (3-6 párrafos) que integre los \
resultados de todas las subtareas en una respuesta coherente para el \
usuario final.

Si alguna subtarea falló o el resultado quedó incompleto, dilo de forma \
explícita y concreta al final: qué falta y por qué. No lo escondas ni lo \
suavices — el usuario necesita saber qué puede usar y qué no.

Si se generaron archivos descargables, menciónalos.

Responde en texto plano, sin JSON, sin backticks."""


class DirectorState(TypedDict):
    job: TaskJob
    plan: DirectorPlan
    on_progress: Callable[[SubTask], None]
    final_summary: str
    budget: TaskBudget
    depth: int
    ronda: int
    veredicto: dict | None


class DirectorAgent(BaseAgent):
    name = "director"

    def __init__(self, agent_factories: dict[TaskType, Callable[[], BaseAgent]] | None = None):
        self.llm = get_llm_client()
        # Inyectado por main.py para evitar import circular (director
        # delega a agentes que viven en el mismo paquete).
        self._agent_factories = agent_factories or {}
        self._sub_agent_cache: dict[TaskType, BaseAgent] = {}
        self._graph = self._build_graph()

    def set_agent_factories(self, factories: dict[TaskType, Callable[[], BaseAgent]]):
        self._agent_factories = factories

    def run(self, job: TaskJob, on_progress: Callable[[SubTask], None] | None = None) -> TaskResult:
        depth = int(job.metadata.get("_depth") or 0)
        budget: TaskBudget = job.metadata.get("_budget") or presupuesto_por_defecto()

        logger.info(
            f"DirectorAgent procesando taskId={job.taskId} profundidad={depth} "
            f"prompt='{job.prompt[:100]}'"
        )

        initial_state: DirectorState = {
            "job": job,
            "plan": DirectorPlan(subtasks=[]),
            "on_progress": on_progress or (lambda _subtask: None),
            "final_summary": "",
            "budget": budget,
            "depth": depth,
            "ronda": 0,
            "veredicto": None,
        }

        try:
            final_state = self._graph.invoke(initial_state)
        except PresupuestoAgotado as exc:
            logger.warning(f"Presupuesto agotado para taskId={job.taskId}: {exc}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.PARTIAL,
                result={"summary": str(exc), "presupuesto": budget.resumen()},
                error=str(exc),
            )
        except Exception as exc:
            logger.exception(f"El grafo del Director falló para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"El Director falló: {exc}",
            )

        plan: DirectorPlan = final_state["plan"]
        veredicto = final_state.get("veredicto")

        # El grafo terminó en decompose: hay plan pero nada ejecutado. Se
        # devuelve para que el usuario lo revise, apruebe o edite.
        if plan.subtasks and all(st.status == SubTaskStatus.PENDING for st in plan.subtasks):
            logger.info(f"taskId={job.taskId} devuelve plan para aprobación")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.AWAITING_APPROVAL,
                result={
                    "summary": (
                        f"Plan propuesto con {len(plan.subtasks)} subtarea(s). "
                        "Revísalo y apruébalo para ejecutarlo; puedes editarlo antes."
                    ),
                    "subtasks": [st.model_dump() for st in plan.subtasks],
                    "presupuesto": budget.resumen(),
                },
            )

        if esta_cancelada(job.taskId):
            limpiar(job.taskId)
            logger.info(f"taskId={job.taskId} cancelada por el usuario")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.CANCELLED,
                result={
                    "summary": final_state.get("final_summary") or "Tarea cancelada por el usuario.",
                    "subtasks": [st.model_dump() for st in plan.subtasks],
                    "presupuesto": budget.resumen(),
                },
                artifacts=[a for st in plan.subtasks for a in st.artifacts],
            )

        hubo_fallos = any(st.status == SubTaskStatus.FAILED for st in plan.subtasks)
        todo_fallo = bool(plan.subtasks) and all(
            st.status == SubTaskStatus.FAILED for st in plan.subtasks
        )
        incumple = bool(veredicto and not veredicto.get("cumple", True))

        status = (
            TaskStatus.FAILED if todo_fallo
            else TaskStatus.PARTIAL if (hubo_fallos or incumple or budget.limitado)
            else TaskStatus.COMPLETED
        )

        # Artefactos de TODAS las subtareas, para que NestJS cree los
        # registros Artifact correspondientes al consolidar.
        artefactos = [a for st in plan.subtasks for a in st.artifacts]

        return TaskResult(
            taskId=job.taskId,
            status=status,
            result={
                "summary": final_state["final_summary"],
                "subtasks": [st.model_dump() for st in plan.subtasks],
                "verificacion": veredicto,
                "presupuesto": budget.resumen(),
            },
            artifacts=artefactos,
        )

    # --- Construcción del grafo ---------------------------------------------

    def _build_graph(self):
        workflow = StateGraph(DirectorState)
        workflow.add_node("decompose", self._decompose_node)
        workflow.add_node("execute", self._execute_node)
        workflow.add_node("verify", self._verify_node)
        workflow.add_node("replan", self._replan_node)
        workflow.add_node("consolidate", self._consolidate_node)

        workflow.set_entry_point("decompose")
        # Tras descomponer se decide si ejecutar o parar a que el usuario
        # apruebe el plan. Parar aquí es barato: solo se gastó la
        # descomposición, ninguna subtarea ha corrido.
        workflow.add_conditional_edges(
            "decompose",
            self._decidir_tras_descomponer,
            {"execute": "execute", "fin": END},
        )
        workflow.add_edge("execute", "verify")
        # El ciclo verify -> replan -> execute es lo que permite corregir
        # un resultado incompleto sin intervención del usuario.
        workflow.add_conditional_edges(
            "verify",
            self._decidir_tras_verificar,
            {"replan": "replan", "consolidate": "consolidate"},
        )
        workflow.add_edge("replan", "execute")
        workflow.add_edge("consolidate", END)

        return workflow.compile()

    # --- Nodos ---------------------------------------------------------------

    def _decompose_node(self, state: DirectorState) -> DirectorState:
        job = state["job"]
        budget = state["budget"]

        # Reanudación: el usuario relanzó una tarea que ya corrió. Se
        # reconstruye el plan anterior con sus resultados, para reintentar
        # SOLO lo que falló. Volver a descomponer gastaría una llamada y,
        # peor, generaría subtareas distintas que ya no encajan con lo
        # completado.
        previas = job.metadata.get("_subtareas_previas")
        if previas:
            plan = DirectorPlan(subtasks=[SubTask.model_validate(s) for s in previas])
            reintentos = 0
            for st in plan.subtasks:
                if st.status == SubTaskStatus.FAILED:
                    st.status = SubTaskStatus.PENDING
                    st.error = None
                    reintentos += 1
            budget.registrar_subtareas(len(plan.subtasks))
            logger.info(
                f"Reanudando taskId={job.taskId}: {reintentos} subtarea(s) a reintentar, "
                f"{len(plan.subtasks) - reintentos} conservada(s)"
            )
            return {**state, "plan": plan}

        # Plan ya aprobado por el usuario (posiblemente editado): se usa
        # tal cual, sin volver a llamar al LLM.
        aprobado = job.metadata.get("_plan_aprobado")
        if aprobado:
            plan = DirectorPlan.model_validate(aprobado)
            plan.subtasks = self._sanear_plan(
                plan.subtasks, MAX_SUBTASKS_PER_PLAN, state["depth"], budget
            )
            budget.registrar_subtareas(len(plan.subtasks))
            logger.info(f"Ejecutando plan aprobado: {len(plan.subtasks)} subtarea(s)")
            return {**state, "plan": plan}

        cupo = min(MAX_SUBTASKS_PER_PLAN, budget.cupo_de_subtareas())
        if cupo <= 0:
            logger.warning("Sin cupo de subtareas: se ejecuta el objetivo como una sola investigación")
            plan = DirectorPlan(
                subtasks=[SubTask(id="t1", agentType=TaskType.RESEARCH, prompt=job.prompt)]
            )
            budget.registrar_subtareas(1)
            return {**state, "plan": plan}

        # Memoria: qué se sabe ya, antes de planificar. Esto es lo que
        # evita re-buscar las mismas empresas en cada ejecución y permite
        # que un objetivo repetido avance en vez de repetirse.
        mensaje = f"OBJETIVO DEL USUARIO: {job.prompt}"
        try:
            recuerdo = resumen_para_contexto(job.prompt)
            if recuerdo:
                mensaje += f"\n\n--- MEMORIA ---\n{recuerdo}"
                logger.info(f"Memoria inyectada al plan ({len(recuerdo)} caracteres)")
        except Exception:
            logger.exception("No se pudo leer la memoria; se planifica sin ella")

        budget.consumir_llamada_llm()
        raw = self.llm.complete(
            system=DECOMPOSE_SYSTEM_PROMPT.format(max_subtareas=cupo),
            user=mensaje,
            max_tokens=2500,
            json_mode=True,
        )
        cleaned = self._strip_markdown_fence(raw)

        try:
            plan = DirectorPlan.model_validate(json.loads(cleaned))
        except Exception:
            logger.exception("El LLM no devolvió un plan válido, usando fallback de una subtarea RESEARCH")
            plan = DirectorPlan(
                subtasks=[SubTask(id="t1", agentType=TaskType.RESEARCH, prompt=job.prompt)]
            )

        plan.subtasks = self._sanear_plan(plan.subtasks, cupo, state["depth"], budget)
        budget.registrar_subtareas(len(plan.subtasks))

        logger.info(f"Plan generado: {len(plan.subtasks)} subtarea(s), cupo era {cupo}")
        return {**state, "plan": plan}

    def _execute_node(self, state: DirectorState) -> DirectorState:
        plan = state["plan"]
        job = state["job"]
        budget = state["budget"]
        on_progress = state["on_progress"]

        subtasks_by_id = {st.id: st for st in plan.subtasks}
        # Se distingue "terminó" de "terminó BIEN": una subtarea cuya
        # dependencia falló no debe ejecutarse con datos que no existen.
        resolved_ids = {st.id for st in plan.subtasks if st.status != SubTaskStatus.PENDING}
        succeeded_ids = {st.id for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED}

        def is_ready(st: SubTask) -> bool:
            return st.status == SubTaskStatus.PENDING and all(
                dep in resolved_ids for dep in st.dependsOn
            )

        def deps_fallidas(st: SubTask) -> list[str]:
            return [d for d in st.dependsOn if d in resolved_ids and d not in succeeded_ids]

        while True:
            if budget.tiempo_agotado():
                logger.warning("Tiempo de tarea agotado: se deja de ejecutar subtareas")
                break

            # Cancelación cooperativa: se comprueba entre oleadas, no
            # dentro de una subtarea en curso. Matar una llamada al LLM ya
            # pagada no ahorra nada; lo que se corta es lo que aún no ha
            # empezado.
            if esta_cancelada(job.taskId):
                logger.info(f"Cancelación detectada en taskId={job.taskId}: no se lanzan más oleadas")
                for st in plan.subtasks:
                    if st.status == SubTaskStatus.PENDING:
                        st.status = SubTaskStatus.FAILED
                        st.error = "Cancelada por el usuario antes de ejecutarse."
                        on_progress(st)
                break

            ready = [st for st in plan.subtasks if is_ready(st)]
            if not ready:
                break

            bloqueadas = [st for st in ready if deps_fallidas(st)]
            for st in bloqueadas:
                fallidas = ", ".join(deps_fallidas(st))
                st.status = SubTaskStatus.FAILED
                st.error = f"No se ejecutó: la subtarea de la que depende ({fallidas}) falló."
                logger.info(f"Subtarea {st.id} omitida por dependencia fallida: {fallidas}")
                on_progress(st)
                resolved_ids.add(st.id)

            ejecutables = [st for st in ready if st not in bloqueadas]
            if not ejecutables:
                continue

            with ThreadPoolExecutor(max_workers=max(len(ejecutables), 1)) as pool:
                futures = {
                    pool.submit(self._run_subtask, st, job, subtasks_by_id, on_progress, state): st
                    for st in ejecutables
                }
                for future in as_completed(futures):
                    st = futures[future]
                    try:
                        future.result()
                    except Exception:
                        logger.exception(f"Subtarea {st.id} lanzó una excepción no controlada")
                    resolved_ids.add(st.id)
                    if st.status == SubTaskStatus.COMPLETED:
                        succeeded_ids.add(st.id)

        # Subtareas que nunca quedaron listas (dependencia inexistente o
        # ciclo en el plan del LLM) se marcan explícitamente.
        for st in plan.subtasks:
            if st.status == SubTaskStatus.PENDING:
                st.status = SubTaskStatus.FAILED
                st.error = "Dependencias no resueltas (posible ciclo o dependencia inexistente en el plan)"
                on_progress(st)

        return {**state, "plan": plan}

    def _decidir_tras_descomponer(self, state: DirectorState) -> str:
        job = state["job"]

        # Un sub-Director nunca pide aprobación: el usuario ya aprobó el
        # plan de la tarea raíz, y detenerse aquí dejaría la tarea padre
        # colgada esperando algo que el usuario no puede ver.
        if state["depth"] > 0:
            return "execute"
        # Se comprueba la PRESENCIA de la clave, no su valor: una lista
        # vacía es falsy, y con `get()` una reanudación sin subtareas
        # volvería a pedir aprobación de un plan que el usuario ya aprobó.
        if "_plan_aprobado" in job.metadata or "_subtareas_previas" in job.metadata:
            return "execute"
        if not REQUIRE_PLAN_APPROVAL:
            return "execute"

        logger.info(f"Plan de taskId={job.taskId} en espera de aprobación")
        return "fin"

    def _verify_node(self, state: DirectorState) -> DirectorState:
        budget = state["budget"]
        plan = state["plan"]
        job = state["job"]

        if not budget.puede_verificar():
            logger.info("Verificación omitida: sin rondas o sin tiempo disponibles")
            return state

        try:
            verificador = self._get_sub_agent(TaskType.VERIFICATION)
        except RuntimeError:
            logger.info("No hay Verificador registrado; se omite la verificación")
            return state

        resultado = {
            "subtasks": [
                {
                    "agentType": st.agentType.value,
                    "status": st.status.value,
                    "prompt": st.prompt,
                    "result": st.result,
                    "error": st.error,
                }
                for st in plan.subtasks
            ]
        }
        artefactos = [a for st in plan.subtasks for a in st.artifacts]

        budget.registrar_ronda_verificacion()
        try:
            budget.consumir_llamada_llm()
            veredicto = verificador.verificar(job.prompt, resultado, artefactos)
        except PresupuestoAgotado:
            logger.info("Sin presupuesto para verificar; se consolida lo obtenido")
            return state
        except Exception:
            logger.exception("La verificación falló; se consolida lo obtenido")
            return state

        return {**state, "veredicto": veredicto.model_dump()}

    def _decidir_tras_verificar(self, state: DirectorState) -> str:
        veredicto = state.get("veredicto")
        budget = state["budget"]

        if not veredicto or veredicto.get("cumple"):
            return "consolidate"
        if not any(
            h.get("accion_sugerida") for h in veredicto.get("huecos", [])
        ):
            return "consolidate"
        if not budget.puede_verificar() or budget.cupo_de_subtareas() <= 0:
            logger.info("Hay huecos pero no queda presupuesto para replanificar")
            return "consolidate"
        if budget.llamadas_restantes() < 4:
            logger.info("Hay huecos pero quedan muy pocas llamadas al LLM; se consolida")
            return "consolidate"
        return "replan"

    def _replan_node(self, state: DirectorState) -> DirectorState:
        plan = state["plan"]
        budget = state["budget"]
        veredicto = state["veredicto"] or {}
        ronda = state["ronda"] + 1

        cupo = min(MAX_SUBTASKS_PER_PLAN, budget.cupo_de_subtareas())
        nuevas: list[SubTask] = []
        existentes = {st.id for st in plan.subtasks}

        for i, hueco in enumerate(veredicto.get("huecos", [])):
            if len(nuevas) >= cupo:
                break
            accion = (hueco.get("accion_sugerida") or "").strip()
            if not accion:
                continue

            try:
                agente = TaskType(hueco.get("agente_sugerido") or TaskType.RESEARCH.value)
            except ValueError:
                agente = TaskType.RESEARCH
            if agente not in self._agent_factories:
                continue

            nuevo_id = f"r{ronda}_{i + 1}"
            while nuevo_id in existentes:
                nuevo_id += "b"
            existentes.add(nuevo_id)

            # Las subtareas de replanificación dependen de las que ya
            # completaron, para que reciban el material recopilado y no
            # repitan trabajo ya hecho.
            depende_de = [
                st.id for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED
            ] if agente in (TaskType.EXTRACTION, TaskType.ANALYSIS,
                            TaskType.SPREADSHEET, TaskType.DOCUMENT,
                            TaskType.PRESENTATION) else []

            nuevas.append(
                SubTask(id=nuevo_id, agentType=agente, prompt=accion,
                        dependsOn=depende_de, ronda=ronda)
            )

        if not nuevas:
            logger.info("La replanificación no produjo subtareas accionables")
            return {**state, "ronda": ronda}

        logger.info(f"Replanificación ronda {ronda}: {len(nuevas)} subtarea(s) nueva(s)")
        budget.registrar_subtareas(len(nuevas))
        plan.subtasks.extend(nuevas)
        for st in nuevas:
            state["on_progress"](st)

        return {**state, "plan": plan, "ronda": ronda}

    def _consolidate_node(self, state: DirectorState) -> DirectorState:
        job = state["job"]
        plan = state["plan"]
        budget = state["budget"]
        veredicto = state.get("veredicto")

        resumen_subtareas = "\n\n".join(
            f"SUBTAREA [{st.agentType.value}] (status={st.status.value}): {st.prompt}\n"
            f"Resultado: {self._recortar(st.result)}\n"
            f"Error: {st.error or 'N/A'}"
            for st in plan.subtasks
        )

        extra = ""
        if veredicto and not veredicto.get("cumple", True):
            huecos = "; ".join(h.get("descripcion", "") for h in veredicto.get("huecos", []))
            extra = f"\n\nLA VERIFICACIÓN DETECTÓ QUE FALTA: {huecos}"
        if budget.limitado:
            extra += (
                "\n\nLA TAREA SE DETUVO POR LÍMITES DE PRESUPUESTO: "
                + "; ".join(budget.resumen()["limitesAlcanzados"])
            )

        try:
            budget.consumir_llamada_llm()
            summary = self.llm.complete(
                system=CONSOLIDATE_SYSTEM_PROMPT,
                user=(
                    f"OBJETIVO ORIGINAL: {job.prompt}\n\n"
                    f"SUBTAREAS EJECUTADAS:\n\n{resumen_subtareas}{extra}"
                ),
                max_tokens=2000,
            )
        except Exception:
            logger.exception("La consolidación final falló, usando resumen generado en código")
            completadas = sum(1 for st in plan.subtasks if st.status == SubTaskStatus.COMPLETED)
            summary = (
                f"Se ejecutaron {len(plan.subtasks)} subtareas, {completadas} completadas. "
                "No fue posible generar el resumen ejecutivo automático; revisa el detalle "
                "de cada subtarea." + extra
            )

        return {**state, "final_summary": summary.strip()}

    # --- Ejecución de una subtarea -------------------------------------------

    def _run_subtask(
        self,
        subtask: SubTask,
        parent_job: TaskJob,
        subtasks_by_id: dict[str, SubTask],
        on_progress: Callable[[SubTask], None],
        state: DirectorState,
    ) -> None:
        budget = state["budget"]
        subtask.status = SubTaskStatus.RUNNING
        on_progress(subtask)

        try:
            agent = self._get_sub_agent(subtask.agentType)

            costo = COSTO_ESTIMADO.get(subtask.agentType, 1)
            if costo:
                budget.consumir_llamada_llm(costo)

            metadata: dict = {}

            # Contexto de TODAS las dependencias, no solo de la primera.
            # Antes se pasaba únicamente dependsOn[0], así que una
            # subtarea que consolidaba varias investigaciones solo veía
            # una de ellas.
            contexto = self._contexto_de_dependencias(subtask, subtasks_by_id)
            if contexto:
                metadata["context_data"] = contexto

            # Archivos aportados por el usuario: se heredan del padre para
            # que una subtarea INGEST sepa qué leer.
            if parent_job.metadata.get("files"):
                metadata["files"] = parent_job.metadata["files"]

            # Descomposición recursiva: un sub-Director hereda el mismo
            # presupuesto (para que el tope sea de la TAREA, no de cada
            # nivel) y una profundidad mayor.
            if subtask.agentType == TaskType.DIRECTOR:
                if not budget.puede_profundizar(state["depth"]):
                    subtask.status = SubTaskStatus.FAILED
                    subtask.error = (
                        f"No se descompuso: se alcanzó la profundidad máxima "
                        f"({budget.max_depth}). Ajusta MAX_DECOMPOSITION_DEPTH si necesitas más."
                    )
                    on_progress(subtask)
                    return
                metadata["_depth"] = state["depth"] + 1
                metadata["_budget"] = budget

            sub_job = TaskJob(
                # IMPORTANTE: se usa el taskId del PADRE, no uno compuesto.
                # Así los agentes que guardan archivos los escriben en
                # /app/artifacts/{taskId}/, la carpeta que NestJS asocia
                # con la tarea visible para el usuario.
                taskId=parent_job.taskId,
                type=subtask.agentType,
                prompt=subtask.prompt,
                userId=parent_job.userId,
                metadata=metadata,
            )

            sub_result = agent.run(sub_job)

            if sub_result.status == TaskStatus.FAILED:
                subtask.status = SubTaskStatus.FAILED
                subtask.error = sub_result.error
            else:
                subtask.status = SubTaskStatus.COMPLETED
                subtask.result = sub_result.result
                subtask.artifacts = sub_result.artifacts
                # Un PARTIAL del sub-agente no es un fallo, pero su aviso
                # debe llegar al Verificador y al usuario.
                if sub_result.status == TaskStatus.PARTIAL and sub_result.error:
                    subtask.error = sub_result.error

        except PresupuestoAgotado as exc:
            logger.warning(f"Subtarea {subtask.id} no se ejecutó: {exc}")
            subtask.status = SubTaskStatus.FAILED
            subtask.error = str(exc)
        except Exception as exc:
            logger.exception(f"Subtarea {subtask.id} (agentType={subtask.agentType}) falló")
            subtask.status = SubTaskStatus.FAILED
            subtask.error = str(exc)

        on_progress(subtask)

    # --- Auxiliares -----------------------------------------------------------

    @staticmethod
    def _contexto_de_dependencias(
        subtask: SubTask, subtasks_by_id: dict[str, SubTask]
    ) -> dict | None:
        """
        Reúne los resultados de todas las dependencias completadas.

        Con una sola dependencia se devuelve su resultado tal cual, para
        no romper a los agentes que ya esperaban ese shape. Con varias se
        devuelve un mapa {id_subtarea: resultado}.
        """
        resultados = {
            dep_id: dep.result
            for dep_id in subtask.dependsOn
            if (dep := subtasks_by_id.get(dep_id)) and dep.result
        }
        if not resultados:
            return None
        if len(resultados) == 1:
            return next(iter(resultados.values()))
        return resultados

    def _sanear_plan(
        self, subtasks: list[SubTask], cupo: int, depth: int, budget: TaskBudget
    ) -> list[SubTask]:
        """
        Corrige lo que el LLM suele equivocar al generar el plan:
        subtareas de más, tipos no registrados, dependencias inexistentes,
        auto-dependencias y recursión cuando ya no queda profundidad.
        """
        saneadas: list[SubTask] = []
        ids_vistos: set[str] = set()

        for st in subtasks[:cupo]:
            if st.id in ids_vistos:
                continue

            # La degradación por profundidad va ANTES de comprobar el
            # registro: una subtarea DIRECTOR que ya no puede profundizar
            # debe convertirse en RESEARCH y ejecutarse, no descartarse.
            if st.agentType == TaskType.DIRECTOR and not budget.puede_profundizar(depth):
                logger.info(f"Subtarea {st.id} era DIRECTOR pero no queda profundidad; pasa a RESEARCH")
                st.agentType = TaskType.RESEARCH

            if st.agentType not in self._agent_factories:
                logger.warning(f"Subtarea {st.id} pide un agente no registrado ({st.agentType}); se omite")
                continue

            ids_vistos.add(st.id)
            saneadas.append(st)

        # Las dependencias que apunten a ids inexistentes se descartan:
        # dejarlas haría que la subtarea nunca quede lista y termine
        # marcada como fallida sin motivo real.
        for st in saneadas:
            st.dependsOn = [d for d in st.dependsOn if d in ids_vistos and d != st.id]

        return saneadas

    @staticmethod
    def _recortar(resultado: dict | None, limite: int = 4000) -> str:
        if not resultado:
            return "N/A"
        texto = json.dumps(resultado, ensure_ascii=False)
        return texto if len(texto) <= limite else texto[:limite] + "…(recortado)"

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
