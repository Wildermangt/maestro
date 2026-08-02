"""
Cliente de la memoria de entidades.

El worker NO toca Postgres directamente: habla con la API de NestJS, igual
que hacen los canales de Telegram/Gmail al crear tareas. NestJS sigue
siendo la única fuente de verdad sobre la base de datos.

Toda operación falla en silencio (registra y sigue). La memoria es una
mejora, no un requisito: si el backend no responde, la tarea debe
completarse igual, solo sin recordar. Tumbar una investigación de varios
minutos porque no se pudo guardar un lead sería un mal negocio.
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger("memory.entity_store")

BACKEND_URL = os.getenv("BACKEND_INTERNAL_URL", "http://backend:4000")
TIMEOUT = 15

# Sinónimos de columna -> campo canónico de la memoria. Los agentes generan
# los encabezados con el LLM, así que varían entre ejecuciones ("Teléfono",
# "Telefono de contacto", "Phone"). Sin este mapeo, la misma empresa se
# guardaría con campos distintos según la corrida.
SINONIMOS = {
    "name": ("empresa", "nombre", "compañ", "compan", "company", "razón social", "razon social"),
    "website": ("sitio", "web", "url", "página", "pagina"),
    "phone": ("tel", "fono", "phone", "celular", "móvil", "movil", "pbx"),
    "email": ("email", "correo", "mail", "e-mail"),
    "address": ("direcc", "address", "ubicac", "sede"),
    "city": ("ciudad", "city", "municipio"),
    "sector": ("sector", "industria", "rubro", "categoría", "categoria"),
    "products": ("producto", "servicio", "línea", "linea", "portafolio", "oferta"),
}


def _peticion(metodo: str, ruta: str, cuerpo: dict | None = None) -> dict | list | None:
    url = f"{BACKEND_URL}{ruta}"
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    req = urllib.request.Request(
        url,
        data=datos,
        headers={"Content-Type": "application/json"} if datos else {},
        method=metodo,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            crudo = r.read().decode("utf-8")
            return json.loads(crudo) if crudo else None
    except urllib.error.HTTPError as exc:
        logger.warning(f"Memoria: {metodo} {ruta} respondió {exc.code}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Memoria no disponible ({metodo} {ruta}): {exc}")
    return None


def _canonizar(fila: dict, columnas: list[str]) -> dict:
    """Traduce una fila con encabezados libres a los campos de la memoria."""
    salida: dict[str, str] = {}
    for columna in columnas:
        valor = str(fila.get(columna, "") or "").strip()
        if not valor:
            continue
        bajo = columna.lower()
        for campo, claves in SINONIMOS.items():
            if campo in salida:
                continue
            if any(k in bajo for k in claves):
                salida[campo] = valor
                break
    return salida


def guardar_tabla(columnas: list[str], filas: list[dict], fuente: str = "") -> dict | None:
    """
    Persiste una tabla producida por el Extractor o el Enriquecedor.

    Devuelve el resumen del backend ({creadas, fusionadas, omitidas}) o
    None si la memoria no estaba disponible.
    """
    entidades = []
    for fila in filas:
        entidad = _canonizar(fila, columnas)
        if not entidad.get("name"):
            continue
        if fuente:
            entidad["sources"] = {"_origen": fuente}
        entidades.append(entidad)

    if not entidades:
        return None

    respuesta = _peticion("POST", "/api/memory/entities", {"entities": entidades})
    if isinstance(respuesta, dict):
        logger.info(
            f"Memoria: {respuesta.get('creadas', 0)} nueva(s), "
            f"{respuesta.get('fusionadas', 0)} fusionada(s)"
        )
    return respuesta if isinstance(respuesta, dict) else None


def recordar_entidades(busqueda: str = "", limite: int = 60) -> list[dict]:
    """Devuelve lo que ya se sabe, para no volver a buscar lo mismo."""
    ruta = f"/api/memory/entities?limit={limite}"
    if busqueda:
        ruta += f"&search={urllib.parse.quote(busqueda[:120])}"
    resultado = _peticion("GET", ruta)
    return resultado if isinstance(resultado, list) else []


def recordar_notas() -> dict[str, str]:
    """Hechos que el usuario le pidió al sistema recordar."""
    resultado = _peticion("GET", "/api/memory/notes")
    if not isinstance(resultado, list):
        return {}
    return {n["key"]: n["value"] for n in resultado if n.get("key")}


def resumen_para_contexto(busqueda: str = "", limite: int = 40) -> str:
    """
    Arma un bloque de texto compacto con lo que ya se sabe, listo para
    inyectar en el prompt de planificación.

    Se devuelve TEXTO y no JSON a propósito: ocupa menos tokens y el
    modelo solo necesita reconocer qué ya tiene, no procesarlo como datos.
    """
    partes: list[str] = []

    notas = recordar_notas()
    if notas:
        partes.append(
            "LO QUE YA SABES DEL USUARIO:\n"
            + "\n".join(f"- {k}: {v}" for k, v in notas.items())
        )

    # El filtro se intenta primero, pero NO se confía en él: `busqueda` suele
    # ser el objetivo completo del usuario ("proveedores de tecnología en
    # Bogotá con sus datos de contacto"), y esa frase entera casi nunca
    # coincide con el nombre o el sector de una empresa. Si filtrar no
    # devuelve nada, se cae a la memoria completa — el objetivo aquí es que
    # el Director SEPA lo que ya tiene, no encontrar coincidencias exactas.
    entidades = recordar_entidades(busqueda, limite) if busqueda else []
    if not entidades:
        entidades = recordar_entidades("", limite)

    if entidades:
        lineas = []
        for e in entidades[:limite]:
            faltantes = [
                etiqueta
                for campo, etiqueta in (("phone", "teléfono"), ("email", "correo"), ("address", "dirección"))
                if not (e.get(campo) or "").strip()
            ]
            estado = e.get("status", "NUEVO")
            hueco = f" — falta {', '.join(faltantes)}" if faltantes else " — completa"
            lineas.append(f"- {e.get('name')} ({e.get('domain')}) [{estado}]{hueco}")
        # El encuadre importa: con "no vuelvas a buscarlas" el modelo
        # entendió lo contrario y planificó INCLUIR estas empresas en los
        # resultados de un objetivo de otro sector (metió distribuidoras
        # de tecnología en una búsqueda de constructoras). Ahora se dice
        # explícitamente que son un historial, no un resultado, y que solo
        # aplican si encajan con el objetivo actual.
        partes.append(
            f"HISTORIAL: empresas que YA encontraste en tareas anteriores "
            f"({len(entidades)}).\n"
            "Esto es un registro de trabajo previo, NO son resultados de la tarea "
            "actual. Úsalo así:\n"
            "- Si alguna encaja con el objetivo de ahora y le faltan datos, "
            "planifica completarlos en vez de volver a buscarla.\n"
            "- Si el objetivo pide MÁS empresas del mismo tipo, busca DISTINTAS "
            "a estas.\n"
            "- Si el objetivo es de otro sector, IGNÓRALAS por completo. Nunca "
            "las incluyas en el resultado solo porque están aquí.\n"
            + "\n".join(lineas)
        )

    return "\n\n".join(partes)
