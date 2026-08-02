import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
QUEUE_NAME = os.getenv("QUEUE_NAME", "tasks-queue")
POLL_TIMEOUT_SECONDS = int(os.getenv("POLL_TIMEOUT_SECONDS") or 5)

# --- Presupuesto por tarea -------------------------------------------------
#
# Sin estos topes, la descomposición recursiva y el bucle de verificación
# pueden disparar el consumo: un solo objetivo grande llegaría a decenas de
# llamadas al LLM. En un tier gratuito eso agota la cuota diaria en una
# sola ejecución, y encima a mitad de camino.
#
# Cuando un tope se alcanza, la tarea NO falla: se corta la expansión y se
# consolida lo que ya se logró (resultado PARTIAL). Es preferible entregar
# algo incompleto y decirlo, a gastar la cuota entera y no entregar nada.
#
# Todos usan `os.getenv(x) or default` porque Compose pasa las variables no
# definidas como cadena vacía, no ausentes.

# Llamadas al LLM que puede consumir UNA tarea, sumando todos sus agentes,
# subtareas y rondas de verificación.
MAX_LLM_CALLS_PER_TASK = int(os.getenv("MAX_LLM_CALLS_PER_TASK") or 40)

# Subtareas totales que puede crear un plan, sumando todos los niveles.
MAX_SUBTASKS_PER_TASK = int(os.getenv("MAX_SUBTASKS_PER_TASK") or 24)

# Subtareas que puede generar UNA sola descomposición. Antes estaba fijo en
# 5 dentro del prompt, lo que impedía trocear objetivos grandes por lotes.
MAX_SUBTASKS_PER_PLAN = int(os.getenv("MAX_SUBTASKS_PER_PLAN") or 8)

# Profundidad de descomposición recursiva. 0 = el Director raíz. Con 2, un
# Director puede delegar en sub-Directores que a su vez delegan en agentes
# de ejecución, pero no más — cada nivel multiplica el costo.
MAX_DECOMPOSITION_DEPTH = int(os.getenv("MAX_DECOMPOSITION_DEPTH") or 2)

# Rondas de verificación + replanificación. 0 desactiva el Verificador.
MAX_VERIFICATION_ROUNDS = int(os.getenv("MAX_VERIFICATION_ROUNDS") or 2)

# Segundos que puede durar una tarea completa antes de consolidar lo que
# haya. Protege contra un proveedor de LLM lento o en reintentos largos.
TASK_TIMEOUT_SECONDS = int(os.getenv("TASK_TIMEOUT_SECONDS") or 900)

# --- Control humano -------------------------------------------------------
#
# Con esto en true, el Director descompone el objetivo y SE DETIENE: deja la
# tarea en AWAITING_APPROVAL con el plan visible, y no ejecuta nada hasta
# que el usuario lo apruebe (pudiendo editarlo antes).
#
# Es la diferencia entre enterarte de que el plan era malo antes de gastar
# la cuota, o después. Cuesta una sola llamada al LLM (la descomposición).
REQUIRE_PLAN_APPROVAL = (os.getenv("REQUIRE_PLAN_APPROVAL") or "false").lower() in ("1", "true", "yes")

# --- Búsqueda web ---------------------------------------------------------
#
# auto    : usa la búsqueda nativa del proveedor de LLM si la soporta, y
#           cae a Tavily/Firecrawl si no.
# native  : fuerza la nativa; falla si el proveedor no la tiene.
# tavily  : fuerza Tavily + Firecrawl (comportamiento original).
#
# La nativa elimina la necesidad de TAVILY_API_KEY y FIRECRAWL_API_KEY:
# pasas de tres claves con tres cuotas distintas a una sola.
SEARCH_PROVIDER = (os.getenv("SEARCH_PROVIDER") or "auto").strip().lower()
