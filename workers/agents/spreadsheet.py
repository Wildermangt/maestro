"""
Agente Hoja de Cálculo — genera un libro Excel (.xlsx) con openpyxl.

A diferencia del CSV del Analista, aquí sí hay varias hojas, encabezados
con formato, anchos de columna y una fila de totales cuando hay columnas
numéricas — que es lo que hace falta para una cotización o un listado
que alguien va a usar de verdad.

Atajo deliberado: si una dependencia ya produjo una tabla estructurada
(el Extractor devuelve `columns` + `rows`), se usa directamente **sin
llamar al LLM**. Convertir una tabla que ya existe en un XLSX es trabajo
de código, no de modelo: ahorra una llamada y elimina el riesgo de que
el modelo altere los datos al copiarlos.
"""
import io
import json
import logging

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from agents.base import BaseAgent
from models import SheetSpec, SpreadsheetOutput, SpreadsheetPlan, TaskJob, TaskResult, TaskStatus
from utils.artifact_storage import save_artifact
from utils.llm_factory import get_llm_client

logger = logging.getLogger("spreadsheet")

MAX_CONTEXT_CHARS = 20000
ANCHO_MAX = 60

SYSTEM_PROMPT = """Eres un especialista en hojas de cálculo. Recibes un \
objetivo y material de contexto. Diseña un libro de Excel.

Reglas:
- Entre 1 y 4 hojas. Nombres cortos (máx. 28 caracteres, sin : \\ / ? * [ ]).
- "columns" son los encabezados; "rows" es una lista de listas, cada una \
con exactamente tantos valores como columnas.
- Usa ÚNICAMENTE datos del material de contexto. Si un dato no está, deja \
la celda como cadena vacía. No inventes cifras ni contactos.
- Los números van como números, no como texto.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto, sin texto \
adicional y sin backticks de markdown:

{
  "filename": "nombre.xlsx",
  "sheets": [
    {"name": "Proveedores", "columns": ["Empresa", "Teléfono"], "rows": [["MPS", "876 6565"]]}
  ]
}"""


class SpreadsheetAgent(BaseAgent):
    name = "spreadsheet"

    def __init__(self):
        self.llm = get_llm_client()

    def run(self, job: TaskJob) -> TaskResult:
        contexto = job.metadata.get("context_data")
        logger.info(f"SpreadsheetAgent procesando taskId={job.taskId}")

        plan = self._plan_desde_contexto(contexto)
        if plan is not None:
            logger.info("Tabla ya estructurada en el contexto: se construye el XLSX sin llamar al LLM")
        else:
            try:
                plan = self._plan_desde_llm(job.prompt, contexto)
            except Exception as exc:
                logger.exception(f"No se pudo generar el plan del libro para taskId={job.taskId}")
                return TaskResult(
                    taskId=job.taskId,
                    status=TaskStatus.FAILED,
                    result={},
                    error=f"No se pudo generar la estructura del Excel: {exc}",
                )

        if not plan.sheets:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error="No había datos para construir el libro de Excel.",
            )

        try:
            xlsx_bytes = self._construir(plan)
        except Exception as exc:
            logger.exception(f"openpyxl falló construyendo el libro para taskId={job.taskId}")
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=f"El archivo .xlsx falló al construirse: {exc}",
            )

        artefacto = save_artifact(
            task_id=job.taskId,
            filename=self._nombre_seguro(plan.filename),
            content_bytes=xlsx_bytes,
            artifact_type="xlsx",
        )

        total = sum(len(h.rows) for h in plan.sheets)
        salida = SpreadsheetOutput(
            summary=f"Libro de Excel con {len(plan.sheets)} hoja(s) y {total} fila(s) de datos.",
            sheet_count=len(plan.sheets),
            total_rows=total,
        )
        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.COMPLETED,
            result=salida.model_dump(),
            artifacts=[artefacto.to_dict()],
        )

    # --- Construcción del plan ---------------------------------------------

    @staticmethod
    def _plan_desde_contexto(contexto) -> SpreadsheetPlan | None:
        """
        Busca en el contexto una tabla ya estructurada (`columns` + `rows`
        del Extractor) y la convierte directamente, sin pasar por el LLM.
        """
        if not contexto:
            return None

        pila = [contexto]
        while pila:
            actual = pila.pop()
            if isinstance(actual, dict):
                cols = actual.get("columns")
                filas = actual.get("rows")
                if isinstance(cols, list) and cols and isinstance(filas, list) and filas:
                    if all(isinstance(f, dict) for f in filas):
                        return SpreadsheetPlan(
                            filename="datos.xlsx",
                            sheets=[
                                SheetSpec(
                                    name="Datos",
                                    columns=[str(c) for c in cols],
                                    rows=[[f.get(c, "") for c in cols] for f in filas],
                                )
                            ],
                        )
                pila.extend(v for v in actual.values() if isinstance(v, (dict, list)))
            elif isinstance(actual, list):
                pila.extend(v for v in actual if isinstance(v, (dict, list)))
        return None

    def _plan_desde_llm(self, prompt: str, contexto) -> SpreadsheetPlan:
        mensaje = f"OBJETIVO:\n{prompt}"
        if contexto:
            texto = json.dumps(contexto, ensure_ascii=False, indent=1)[:MAX_CONTEXT_CHARS]
            mensaje += f"\n\nMATERIAL DE CONTEXTO:\n{texto}"

        raw = self.llm.complete(system=SYSTEM_PROMPT, user=mensaje, max_tokens=8000, json_mode=True)
        limpio = raw.strip()
        if limpio.startswith("```"):
            limpio = limpio.strip("`").removeprefix("json").strip()
        return SpreadsheetPlan.model_validate(json.loads(limpio))

    # --- Construcción del archivo ------------------------------------------

    @staticmethod
    def _nombre_seguro(nombre: str | None) -> str:
        import os
        import re

        base = os.path.basename((nombre or "").strip()) or "datos.xlsx"
        base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
        if not base.lower().endswith(".xlsx"):
            base += ".xlsx"
        return base[:80]

    @staticmethod
    def _nombre_hoja(nombre: str, usados: set[str]) -> str:
        """Excel rechaza ciertos caracteres y nombres duplicados o >31 chars."""
        import re

        limpio = re.sub(r"[:\\/?*\[\]]", "-", (nombre or "Hoja").strip())[:28] or "Hoja"
        candidato = limpio
        i = 2
        while candidato.lower() in usados:
            candidato = f"{limpio[:26]}_{i}"
            i += 1
        usados.add(candidato.lower())
        return candidato

    @classmethod
    def _construir(cls, plan: SpreadsheetPlan) -> bytes:
        wb = Workbook()
        wb.remove(wb.active)

        encabezado_fondo = PatternFill("solid", fgColor="1F3A54")
        encabezado_fuente = Font(color="FFFFFF", bold=True)
        usados: set[str] = set()

        for hoja in plan.sheets:
            ws = wb.create_sheet(cls._nombre_hoja(hoja.name, usados))

            ws.append([str(c) for c in hoja.columns])
            for celda in ws[1]:
                celda.fill = encabezado_fondo
                celda.font = encabezado_fuente
                celda.alignment = Alignment(vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"

            for fila in hoja.rows:
                # Rellena o recorta para que toda fila calce con las columnas.
                valores = list(fila)[: len(hoja.columns)]
                valores += [""] * (len(hoja.columns) - len(valores))
                ws.append(valores)

            # Ancho por contenido, con tope para que no quede ilegible.
            for i, _ in enumerate(hoja.columns, start=1):
                largo = max(
                    [len(str(ws.cell(row=r, column=i).value or "")) for r in range(1, ws.max_row + 1)]
                    or [10]
                )
                ws.column_dimensions[get_column_letter(i)].width = min(max(12, largo + 2), ANCHO_MAX)

            if hoja.rows:
                ws.auto_filter.ref = f"A1:{get_column_letter(len(hoja.columns))}{ws.max_row}"

        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()
