"""
Agente Extractor — convierte fuentes en filas estructuradas.

Es la pieza que faltaba entre el Investigador y el Analista. El
Investigador devuelve prosa (resumen + hallazgos); el Analista necesita
datos tabulares. Sin este agente en medio, el Analista tenía que
re-deducir las filas leyendo un resumen narrativo, y por eso los CSV
salían pobres o inventados.

El Extractor recibe el contexto de sus dependencias (los resultados del
Investigador, incluidas las fuentes crudas) y produce `columns` + `rows`
validados con Pydantic.

Regla central: **no inventa**. Si un campo no aparece en las fuentes, lo
deja vacío. Un teléfono inventado es peor que un campo en blanco, porque
el usuario no puede distinguirlo de uno real.
"""
import json
import logging

from agents.base import BaseAgent
from memory.entity_store import guardar_tabla
from models import ExtractionOutput, TaskJob, TaskResult, TaskStatus
from utils.llm_factory import get_llm_client

logger = logging.getLogger("extractor")

# Tope de contexto para no agotar el presupuesto de salida del modelo.
MAX_CONTEXT_CHARS = 24000
MAX_TOKENS = 8000

SYSTEM_PROMPT = """Eres un Extractor de datos. Recibes un objetivo y \
material de origen (resultados de investigación, contenido de páginas \
web, texto de documentos). Tu trabajo es devolver una TABLA.

Reglas:
1. Deduce las columnas del objetivo. Si el usuario pidió "datos de \
contacto y productos", las columnas naturales son algo como: Empresa, \
Sitio web, Dirección, Teléfono, Email, Productos.
2. Una fila por entidad encontrada. Usa exactamente las mismas claves \
en todas las filas.
3. **NO INVENTES NADA.** Si un dato no está en el material, deja el \
campo como cadena vacía "". Nunca rellenes con ejemplos plausibles, \
"N/D", "no disponible" ni valores inventados. Un campo vacío es \
información honesta; un dato inventado es un error que el usuario no \
puede detectar.
4. No repitas entidades. Si la misma empresa aparece en varias fuentes, \
fusiona sus datos en una sola fila.
5. Si el objetivo pide una cantidad concreta ("los 50 más grandes"), \
ponla en "expected_count" aunque hayas encontrado menos. Extrae todas \
las que realmente estén en el material; no completes hasta el número.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto, sin texto \
adicional y sin backticks de markdown:

{
  "columns": ["Empresa", "Sitio web", "Teléfono", ...],
  "rows": [{"Empresa": "...", "Sitio web": "...", "Teléfono": "", ...}, ...],
  "summary": "string: qué se extrajo, de cuántas fuentes, y qué campos quedaron incompletos",
  "expected_count": 50
}"""


class ExtractorAgent(BaseAgent):
    name = "extractor"

    def __init__(self):
        self.llm = get_llm_client()

    def run(self, job: TaskJob) -> TaskResult:
        prompt = job.prompt
        contexto = job.metadata.get("context_data")
        logger.info(f"ExtractorAgent procesando taskId={job.taskId} prompt='{prompt[:80]}'")

        if not contexto:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=(
                    "El Extractor no recibió material de origen. Debe depender de una "
                    "subtarea previa (RESEARCH o INGEST) que aporte el contenido a estructurar."
                ),
            )

        material = self._preparar_material(contexto)
        user_message = f"OBJETIVO:\n{prompt}\n\nMATERIAL DE ORIGEN:\n{material}"

        try:
            raw = self.llm.complete(system=SYSTEM_PROMPT, user=user_message, max_tokens=MAX_TOKENS, json_mode=True)
            salida = self._parsear(raw)
        except Exception as exc:
            logger.exception(f"La extracción falló para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"No se pudieron estructurar los datos: {exc}",
            )

        salida = self._limpiar(salida)

        # Si se pidió una cantidad y se extrajo menos, la tarea se marca
        # PARTIAL en vez de COMPLETED: es información que el Verificador
        # y el usuario necesitan, y que un COMPLETED escondería.
        estado = TaskStatus.COMPLETED
        aviso = None
        if salida.expected_count and len(salida.rows) < salida.expected_count:
            estado = TaskStatus.PARTIAL
            aviso = (
                f"Se pidieron {salida.expected_count} registros y solo se encontraron "
                f"{len(salida.rows)} en las fuentes disponibles."
            )

        logger.info(f"Extraídas {len(salida.rows)} fila(s) con {len(salida.columns)} columna(s)")

        # Persistir en la memoria de entidades. Falla en silencio a
        # propósito: perder la investigación de varios minutos porque el
        # backend no respondió sería un mal negocio.
        guardar_tabla(salida.columns, salida.rows, fuente=f"extractor:{job.taskId}")

        return TaskResult(
            taskId=job.taskId,
            status=estado,
            result=salida.model_dump(),
            error=aviso,
        )

    @staticmethod
    def _preparar_material(contexto) -> str:
        """
        Aplana el contexto de las dependencias a texto. Acepta tanto el
        dict de una sola dependencia como el mapa {id: resultado} que
        el Director pasa cuando hay varias.
        """
        texto = json.dumps(contexto, ensure_ascii=False, indent=1)
        if len(texto) > MAX_CONTEXT_CHARS:
            logger.warning(
                f"Material de {len(texto)} caracteres recortado a {MAX_CONTEXT_CHARS} "
                "para no agotar el presupuesto de salida del modelo"
            )
            texto = texto[:MAX_CONTEXT_CHARS] + "\n…(material recortado)"
        return texto

    @staticmethod
    def _parsear(raw: str) -> ExtractionOutput:
        limpio = raw.strip()
        if limpio.startswith("```"):
            limpio = limpio.strip("`").removeprefix("json").strip()
        return ExtractionOutput.model_validate(json.loads(limpio))

    @staticmethod
    def _limpiar(salida: ExtractionOutput) -> ExtractionOutput:
        """
        Normaliza la tabla: descarta filas vacías, unifica las claves de
        todas las filas contra `columns` y elimina duplicados exactos.
        Un modelo puede omitir una clave en algunas filas, y eso rompería
        el CSV que se genere después.
        """
        columnas = salida.columns or (list(salida.rows[0].keys()) if salida.rows else [])

        vistas: set[str] = set()
        filas: list[dict] = []
        for fila in salida.rows:
            normal = {col: str(fila.get(col, "") or "").strip() for col in columnas}
            if not any(normal.values()):
                continue  # fila completamente vacía
            firma = "|".join(normal.get(c, "").lower() for c in columnas[:2])
            if firma in vistas:
                continue  # duplicado por las dos primeras columnas
            vistas.add(firma)
            filas.append(normal)

        salida.columns = columnas
        salida.rows = filas
        return salida
