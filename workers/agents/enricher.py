"""
Agente Enriquecedor — completa los datos de contacto visitando cada sitio.

Resuelve el hueco que dejó la prueba real: el Investigador sabe BUSCAR en
la web, pero no sabe VISITAR una lista concreta de sitios y sacar su ficha
de contacto. Cuando se le pedía "busca el teléfono de estas 13 empresas",
hacía una búsqueda genérica y volvía con datos inútiles sobre cuántas
empresas hay registradas en Bogotá.

Este agente recibe la tabla del Extractor, y por cada fila con sitio web:

  1. Scrapea la home y, si existe, la página de contacto (/contacto,
     /contactenos, /contact...). Son llamadas a Firecrawl, no al LLM.
  2. Extrae correos y teléfonos con **expresiones regulares**. Es gratis,
     determinista y no puede alucinar un teléfono: si el número no está
     literalmente en la página, no aparece.
  3. Una ÚNICA llamada al LLM, con el material de todas las empresas
     juntas, para las direcciones físicas — que sí requieren criterio
     porque no tienen un formato regular.

El coste es N×2 scrapes + 1 llamada al LLM, no N llamadas. Esa diferencia
es lo que lo hace viable con una cuota diaria pequeña.
"""
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

from agents.base import BaseAgent
from memory.entity_store import guardar_tabla
from models import ExtractionOutput, TaskJob, TaskResult, TaskStatus
from tools.web_scraper import WebScraperTool
from utils.llm_factory import get_llm_client

logger = logging.getLogger("enricher")

MAX_EMPRESAS = 20
MAX_HILOS = 5

# Rutas de contacto en el orden en que conviene probarlas. Se incluyen las
# variantes colombianas ("contactenos", "contactanos"): el diagnóstico
# sobre sitios reales mostró que /contacto es la que más veces acierta,
# pero no la única.
RUTAS_CONTACTO = (
    "/contacto",
    "/contactenos",
    "/contactanos",
    "/contact",
    "/contact-us",
    "/es/contacto",
    "/nosotros",
    "/quienes-somos",
)

# Presupuesto de texto por empresa. Estaba en 3500, y como la home suele
# ocupar 6000 ella sola, la página de contacto se descartaba entera —
# justo la que trae los datos. Ahora caben varias páginas.
MAX_CHARS_POR_EMPRESA = 12000

# Correo: se excluyen los de ejemplo y los de plataformas que aparecen en
# plantillas de sitios web, que si no contaminan casi todos los resultados.
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_BASURA = (
    "example.com", "sentry.io", "wixpress.com", "godaddy", "sentry-next",
    "domain.com", "email.com", "yoursite", "tudominio", "@2x", ".png", ".jpg",
    # Servicios de notificación de terceros: aparecen en los pies de página
    # de muchos sitios colombianos, pero no son el correo de la empresa.
    # En una corrida real se recogió experienciadeservicio@notificaciones.co
    # como si fuera el contacto de una constructora.
    "notificaciones.co", "notificacion.co", "mailer", "no-reply", "noreply",
    "sendgrid", "mailchimp", "hubspot", "wordpress",
)

# Teléfonos de Colombia: móvil (3XX XXX XXXX), fijo con indicativo nuevo
# (60X XXX XXXX), y formatos con +57 o paréntesis. Se pide un mínimo de
# 7 dígitos para no capturar años, precios ni códigos postales.
RE_TELEFONO = re.compile(
    r"(?:\+?57[\s.-]?)?(?:\(?\d{1,3}\)?[\s.-]?)?\d{3}[\s.-]?\d{2,4}[\s.-]?\d{2,4}"
)

SYSTEM_PROMPT = """Eres un extractor de direcciones físicas. Recibes, por \
cada empresa, texto extraído de su sitio web.

Devuelve la dirección física de su sede (calle, carrera, avenida, número, \
oficina, ciudad). Reglas:

- Usa SOLO lo que aparezca literalmente en el texto de esa empresa. Si no \
hay dirección, devuelve cadena vacía "". Nunca inventes ni deduzcas una \
dirección a partir del nombre o la ciudad.
- Si hay varias sedes, devuelve la de Bogotá si existe; si no, la primera.
- Devuelve la dirección limpia, en una línea, sin etiquetas como \
"Dirección:" ni teléfonos mezclados.

Responde ÚNICAMENTE con un objeto JSON con este shape exacto:

{"direcciones": {"Nombre exacto de la empresa": "Cra 7 # 71-21 Torre B, Bogotá", "Otra": ""}}"""


class EnricherAgent(BaseAgent):
    name = "enricher"

    def __init__(self):
        self.scraper = WebScraperTool()
        self.llm = get_llm_client()

    def run(self, job: TaskJob) -> TaskResult:
        contexto = job.metadata.get("context_data")
        tabla = self._buscar_tabla(contexto)

        if not tabla:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={},
                error=(
                    "El Enriquecedor no recibió una tabla que completar. Debe depender de "
                    "una subtarea EXTRACTION que haya producido 'columns' y 'rows'."
                ),
            )

        columnas, filas = tabla
        col_nombre = self._columna(columnas, ("empresa", "nombre", "compañ", "company"))
        col_sitio = self._columna(columnas, ("sitio", "web", "url", "página", "pagina"))

        if not col_sitio:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={"columns": columnas, "rows": filas},
                error="La tabla no tiene una columna de sitio web; no hay dónde buscar los contactos.",
            )

        col_nombre = col_nombre or columnas[0]
        objetivo = [f for f in filas[:MAX_EMPRESAS] if self._url_valida(f.get(col_sitio))]
        logger.info(f"Enriqueciendo {len(objetivo)} de {len(filas)} empresas (columna sitio: '{col_sitio}')")

        if not objetivo:
            return TaskResult(
                taskId=job.taskId,
                status=TaskStatus.FAILED,
                result={"columns": columnas, "rows": filas},
                error="Ninguna fila tiene un sitio web válido que visitar.",
            )

        # 1) Scraping en paralelo. Solo Firecrawl, sin coste de LLM.
        contenidos = self._scrapear_todas(objetivo, col_nombre, col_sitio)

        # 2) Correos y teléfonos por regex: determinista y gratis.
        col_email = self._columna(columnas, ("email", "correo", "mail")) or "Email"
        col_tel = self._columna(columnas, ("tel", "fono", "phone", "celular")) or "Teléfono"
        col_dir = self._columna(columnas, ("direcc", "address", "ubicac")) or "Dirección"
        for c in (col_email, col_tel, col_dir):
            if c not in columnas:
                columnas.append(c)

        rellenados = {"email": 0, "telefono": 0, "direccion": 0}
        for fila in objetivo:
            texto = contenidos.get(fila.get(col_nombre, ""), "")
            if not texto:
                continue
            dominio = urlparse(str(fila.get(col_sitio))).netloc.lower()

            if not str(fila.get(col_email) or "").strip():
                correo = self._mejor_email(texto, dominio)
                if correo:
                    fila[col_email] = correo
                    rellenados["email"] += 1

            if not str(fila.get(col_tel) or "").strip():
                telefono = self._mejor_telefono(texto)
                if telefono:
                    fila[col_tel] = telefono
                    rellenados["telefono"] += 1

        # 3) Direcciones: una sola llamada al LLM con todo el material.
        try:
            direcciones = self._extraer_direcciones(contenidos)
            for fila in objetivo:
                if str(fila.get(col_dir) or "").strip():
                    continue
                d = (direcciones.get(fila.get(col_nombre, "")) or "").strip()
                if d:
                    fila[col_dir] = d
                    rellenados["direccion"] += 1
        except Exception:
            logger.exception("No se pudieron extraer las direcciones; el resto del enriquecimiento se conserva")

        resumen = (
            f"Se visitaron {len(contenidos)} de {len(objetivo)} sitios. Campos completados: "
            f"{rellenados['email']} correos, {rellenados['telefono']} teléfonos, "
            f"{rellenados['direccion']} direcciones."
        )
        logger.info(resumen)

        # Se persiste DESPUÉS de enriquecer: así la memoria recibe la
        # versión con contactos, y la fusión del backend rellena los huecos
        # que dejó la extracción inicial de esta misma empresa.
        guardar_tabla(columnas, filas, fuente=f"enricher:{job.taskId}")

        vacios = sum(
            1 for f in filas for c in (col_email, col_tel, col_dir)
            if not str(f.get(c) or "").strip()
        )
        salida = ExtractionOutput(columns=columnas, rows=filas, summary=resumen)

        return TaskResult(
            taskId=job.taskId,
            status=TaskStatus.PARTIAL if vacios else TaskStatus.COMPLETED,
            result=salida.model_dump(),
            error=(
                f"Quedaron {vacios} campos de contacto sin dato: no aparecían en los sitios visitados."
                if vacios else None
            ),
        )

    # --- Scraping ------------------------------------------------------------

    def _scrapear_todas(self, filas: list[dict], col_nombre: str, col_sitio: str) -> dict[str, str]:
        contenidos: dict[str, str] = {}

        def trabajo(fila: dict) -> tuple[str, str]:
            nombre = str(fila.get(col_nombre, "")).strip()
            url = str(fila.get(col_sitio, "")).strip()
            return nombre, self._scrapear_empresa(url)

        with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
            futuros = {pool.submit(trabajo, f): f for f in filas}
            for futuro in as_completed(futuros):
                try:
                    nombre, texto = futuro.result()
                    if texto:
                        contenidos[nombre] = texto[:MAX_CHARS_POR_EMPRESA]
                except Exception:
                    logger.exception("Fallo scrapeando una empresa; se continúa con las demás")
        return contenidos

    def _scrapear_empresa(self, url: str) -> str:
        """
        Página de contacto primero, home después.

        El orden importa y no es cosmético: el texto de todas las páginas se
        concatena y luego se recorta a MAX_CHARS_POR_EMPRESA. Con la home
        delante —que suele ocupar el tope entero— la página de contacto se
        descartaba completa. En una corrida real, ingeurbe.com/contacto
        tenía 5 correos y un teléfono, y la fila salió vacía por esto.
        """
        base = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}"
        partes: list[str] = []

        # Se recorren TODAS las rutas de contacto, no solo hasta la primera
        # que responda: distintos sitios reparten el correo y el teléfono
        # entre "contacto" y "nosotros", y un scrape más cuesta poco
        # comparado con dejar la fila vacía.
        for ruta in RUTAS_CONTACTO:
            texto = self._leer(urljoin(base, ruta))
            if texto:
                partes.append(texto)
            if sum(len(p) for p in partes) >= MAX_CHARS_POR_EMPRESA:
                break

        if sum(len(p) for p in partes) < MAX_CHARS_POR_EMPRESA:
            home = self._leer(url)
            if home:
                partes.append(home)

        return "\n".join(partes)

    def _leer(self, url: str) -> str | None:
        """
        Firecrawl primero; si no devuelve nada, petición HTTP directa.

        El diagnóstico mostró que Firecrawl no alcanzó 5 de 9 sitios de
        constructoras. El respaldo directo no renderiza JavaScript, pero
        muchos sitios sirven el pie de página con teléfono y correo en el
        HTML inicial — y no consume cuota de ninguna API.
        """
        texto = self.scraper.scrape(url)
        if texto:
            return texto
        return self._leer_directo(url)

    @staticmethod
    def _leer_directo(url: str) -> str | None:
        try:
            import requests

            r = requests.get(
                url,
                timeout=12,
                allow_redirects=True,
                headers={
                    # Sin User-Agent de navegador, muchos sitios devuelven
                    # 403 a cualquier cliente automatizado.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                    ),
                    "Accept-Language": "es-CO,es;q=0.9",
                },
            )
            if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
                return None
            # Quitar script/style antes de desetiquetar: si no, el JS
            # embebido llena el texto de ruido y de números que parecen
            # teléfonos.
            html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
            texto = re.sub(r"(?s)<[^>]+>", " ", html)
            texto = re.sub(r"&nbsp;?", " ", texto)
            return re.sub(r"\s+", " ", texto).strip() or None
        except Exception:
            logger.debug(f"Lectura directa falló para {url}")
            return None

    # --- Extracción determinista ----------------------------------------------

    @staticmethod
    def _mejor_email(texto: str, dominio: str) -> str:
        candidatos = []
        for correo in RE_EMAIL.findall(texto):
            bajo = correo.lower()
            if any(b in bajo for b in EMAIL_BASURA):
                continue
            candidatos.append(correo)
        if not candidatos:
            return ""
        # Prioridad: correo del propio dominio, luego los genéricos de contacto.
        raiz = dominio.replace("www.", "")
        propios = [c for c in candidatos if raiz and raiz in c.lower()]
        pool = propios or candidatos
        for prefijo in ("contacto", "ventas", "info", "comercial", "servicioalcliente"):
            for c in pool:
                if c.lower().startswith(prefijo):
                    return c
        return pool[0]

    @staticmethod
    def _telefono_valido(digitos: str) -> bool:
        """
        Valida contra el plan de numeración de Colombia.

        Antes solo se comprobaba la LONGITUD (7 a 13 dígitos), y eso dejaba
        pasar cualquier número de la página. En una prueba real se colaron
        '1783713605' y '1785241002' —probablemente identificadores de la
        web— como si fueran teléfonos. En una lista de prospección eso es
        peor que un campo vacío: llamas a un número equivocado y no tienes
        forma de saber que el dato era basura.
        """
        # Quitar el indicativo de país si viene.
        if digitos.startswith("57") and len(digitos) in (11, 12):
            digitos = digitos[2:]

        if len(digitos) == 10:
            # Móvil: empieza en 3. Fijo con indicativo nuevo: empieza en 60.
            return digitos[0] == "3" or digitos.startswith("60")
        if len(digitos) == 7:
            # Fijo en formato antiguo: no empieza en 0 ni en 1.
            return digitos[0] not in "01"
        return False

    @classmethod
    def _mejor_telefono(cls, texto: str) -> str:
        for bruto in RE_TELEFONO.findall(texto):
            digitos = re.sub(r"\D", "", bruto)
            if cls._telefono_valido(digitos):
                return bruto.strip()
        return ""

    # --- Direcciones (una sola llamada al LLM) ---------------------------------

    def _extraer_direcciones(self, contenidos: dict[str, str]) -> dict[str, str]:
        if not contenidos:
            return {}

        bloques = "\n\n".join(
            f"=== EMPRESA: {nombre} ===\n{texto[:2500]}" for nombre, texto in contenidos.items()
        )
        raw = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=f"MATERIAL DE LOS SITIOS WEB:\n\n{bloques}",
            max_tokens=3000,
            json_mode=True,
        )
        limpio = raw.strip()
        if limpio.startswith("```"):
            limpio = limpio.strip("`").removeprefix("json").strip()
        datos = json.loads(limpio)
        return datos.get("direcciones", {}) if isinstance(datos, dict) else {}

    # --- Auxiliares -------------------------------------------------------------

    @staticmethod
    def _url_valida(valor) -> bool:
        s = str(valor or "").strip()
        return s.startswith(("http://", "https://")) and "." in urlparse(s).netloc

    @staticmethod
    def _columna(columnas: list[str], claves: tuple[str, ...]) -> str | None:
        for col in columnas:
            bajo = col.lower()
            if any(k in bajo for k in claves):
                return col
        return None

    @staticmethod
    def _buscar_tabla(contexto) -> tuple[list[str], list[dict]] | None:
        """Encuentra la tabla más completa dentro del contexto recibido."""
        if not contexto:
            return None
        mejor = None
        pila = [contexto]
        while pila:
            actual = pila.pop()
            if isinstance(actual, dict):
                cols, filas = actual.get("columns"), actual.get("rows")
                if (isinstance(cols, list) and cols and isinstance(filas, list)
                        and filas and all(isinstance(f, dict) for f in filas)):
                    if mejor is None or len(filas) > len(mejor[1]):
                        mejor = (list(cols), [dict(f) for f in filas])
                pila.extend(v for v in actual.values() if isinstance(v, (dict, list)))
            elif isinstance(actual, list):
                pila.extend(v for v in actual if isinstance(v, (dict, list)))
        return mejor
