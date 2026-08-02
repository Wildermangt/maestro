# -*- coding: utf-8 -*-
"""
Servidor local del Panel de Control de Maestro.

Solo librería estándar: no añade dependencias al proyecto. Sirve el panel
y expone una API mínima para consultar el estado del stack, levantarlo y
detenerlo, y encargar trabajo a la API de Maestro.

Regla de seguridad: este servidor NUNCA devuelve el valor de una variable
de entorno. De las claves solo informa si están configuradas o no.

Uso:
    python panel/servidor.py          # http://127.0.0.1:8770
"""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PANEL_DIR = Path(__file__).resolve().parent
PROYECTO_DIR = PANEL_DIR.parent
PUERTO = 8770
API_MAESTRO = os.getenv("MAESTRO_API_URL", "http://127.0.0.1:4000")

# Servicios declarados en docker-compose.yml, en el orden real del flujo de
# una petición. El panel dibuja la ruta con este mismo orden.
SERVICIOS = [
    {"id": "frontend", "nombre": "Frontend", "detalle": "Next.js 15", "puerto": 3000},
    {"id": "backend", "nombre": "API", "detalle": "NestJS", "puerto": 4000},
    {"id": "redis", "nombre": "Cola", "detalle": "Redis + BullMQ", "puerto": 6379},
    {"id": "worker", "nombre": "Worker", "detalle": "Agentes Python", "puerto": None},
    {"id": "postgres", "nombre": "Base de datos", "detalle": "PostgreSQL 16", "puerto": 5432},
    {"id": "qdrant", "nombre": "Memoria", "detalle": "Qdrant vectorial", "puerto": 6333},
]

# Claves que el panel reporta como configuradas / faltantes. Nunca su valor.
CLAVES = [
    ("LLM_PROVIDER", "Proveedor de LLM", False),
    ("ANTHROPIC_API_KEY", "Clave de Anthropic", False),
    ("OPENAI_API_KEY", "Clave de OpenAI", False),
    ("GEMINI_API_KEY", "Clave de Gemini", False),
    ("TAVILY_API_KEY", "Búsqueda web (Tavily)", True),
    ("FIRECRAWL_API_KEY", "Lectura de fuentes (Firecrawl)", True),
    ("VERCEL_TOKEN", "Despliegue a Vercel", False),
]

_tarea_en_curso = {"activa": False, "accion": None, "salida": []}
_lock = threading.Lock()


# --------------------------------------------------------------- utilidades

def _correr(args, timeout=25):
    """Ejecuta un comando y devuelve (ok, salida). Nunca lanza excepción."""
    try:
        proc = subprocess.run(
            args,
            cwd=str(PROYECTO_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return False, "comando no encontrado"
    except subprocess.TimeoutExpired:
        return False, "tiempo de espera agotado"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _docker_disponible():
    ok, salida = _correr(["docker", "version", "--format", "{{.Server.Version}}"], timeout=12)
    if ok:
        return True, salida.strip()
    if "comando no encontrado" in salida:
        return False, "Docker no está instalado o no está en el PATH."
    return False, "Docker está instalado pero el motor no responde. Abre Docker Desktop."


def _estado_servicios():
    """Lee `docker compose ps` y mapea el estado de cada servicio."""
    ok, salida = _correr(["docker", "compose", "ps", "--format", "json"], timeout=25)
    estados = {}
    if not ok:
        return estados

    # Según la versión, la salida es un array JSON o un objeto por línea.
    bruto = salida.strip()
    registros = []
    if bruto.startswith("["):
        try:
            registros = json.loads(bruto)
        except json.JSONDecodeError:
            registros = []
    else:
        for linea in bruto.splitlines():
            linea = linea.strip()
            if not linea.startswith("{"):
                continue
            try:
                registros.append(json.loads(linea))
            except json.JSONDecodeError:
                continue

    for r in registros:
        nombre = r.get("Service") or r.get("Name") or ""
        estado = (r.get("State") or r.get("Status") or "").lower()
        if "running" in estado or "up" in estado:
            valor = "activo"
        elif "exited" in estado or "dead" in estado:
            valor = "detenido"
        elif "restarting" in estado or "starting" in estado or "created" in estado:
            valor = "arrancando"
        else:
            valor = "desconocido"
        estados[nombre] = {"estado": valor, "crudo": r.get("Status") or ""}
    return estados


def _leer_env():
    """
    Devuelve qué claves están configuradas, SIN exponer ningún valor.
    Lee el .env del proyecto si existe; si no, cae al entorno del proceso.
    """
    valores = {}
    ruta = PROYECTO_DIR / ".env"
    if ruta.exists():
        try:
            for linea in ruta.read_text(encoding="utf-8", errors="replace").splitlines():
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                k, v = linea.split("=", 1)
                valores[k.strip()] = v.strip()
        except OSError:
            pass

    resultado = []
    for clave, etiqueta, requerida in CLAVES:
        v = valores.get(clave, os.getenv(clave, "") or "")
        resultado.append(
            {
                "clave": clave,
                "etiqueta": etiqueta,
                "configurada": bool(v),
                "requerida": requerida,
                # El proveedor no es un secreto: es la única que se muestra.
                "valor": v if clave == "LLM_PROVIDER" and v else None,
            }
        )
    return {"existe_env": ruta.exists(), "claves": resultado}


def _api_alcanzable():
    try:
        with urllib.request.urlopen(f"{API_MAESTRO}/api/tasks", timeout=3) as r:
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


def _lanzar_en_segundo_plano(accion, args):
    """Corre docker compose up/down sin bloquear la respuesta HTTP."""
    def tarea():
        with _lock:
            _tarea_en_curso["activa"] = True
            _tarea_en_curso["accion"] = accion
            _tarea_en_curso["salida"] = [f"$ {' '.join(args)}", ""]
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(PROYECTO_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for linea in proc.stdout:
                with _lock:
                    _tarea_en_curso["salida"].append(linea.rstrip())
                    # Cota de memoria: el build de Docker es muy verboso.
                    if len(_tarea_en_curso["salida"]) > 400:
                        del _tarea_en_curso["salida"][:100]
            proc.wait()
            with _lock:
                _tarea_en_curso["salida"].append(f"\n— proceso terminado (código {proc.returncode}) —")
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _tarea_en_curso["salida"].append(f"Error: {exc}")
        finally:
            with _lock:
                _tarea_en_curso["activa"] = False

    threading.Thread(target=tarea, daemon=True).start()


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    server_version = "PanelMaestro/1.0"

    def log_message(self, formato, *args):  # silencio en consola
        pass

    # -- helpers

    def _json(self, datos, codigo=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _archivo(self, ruta, tipo):
        try:
            cuerpo = ruta.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cuerpo_json(self):
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if largo <= 0 or largo > 100_000:
                return {}
            return json.loads(self.rfile.read(largo).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # -- rutas

    def do_GET(self):
        ruta = self.path.split("?")[0]

        if ruta in ("/", "/index.html"):
            self._archivo(PANEL_DIR / "index.html", "text/html; charset=utf-8")
            return

        if ruta == "/api/estado":
            docker_ok, docker_msg = _docker_disponible()
            estados = _estado_servicios() if docker_ok else {}
            servicios = []
            for s in SERVICIOS:
                info = estados.get(s["id"], {})
                servicios.append({**s, "estado": info.get("estado", "detenido")})
            self._json(
                {
                    "docker": {"disponible": docker_ok, "mensaje": docker_msg},
                    "servicios": servicios,
                    "configuracion": _leer_env(),
                    "api_alcanzable": _api_alcanzable(),
                    "ocupado": _tarea_en_curso["activa"],
                    "accion": _tarea_en_curso["accion"],
                }
            )
            return

        if ruta == "/api/salida":
            with _lock:
                self._json({"activa": _tarea_en_curso["activa"], "lineas": list(_tarea_en_curso["salida"])})
            return

        if ruta == "/api/encargos":
            try:
                with urllib.request.urlopen(f"{API_MAESTRO}/api/tasks", timeout=5) as r:
                    self._json({"ok": True, "encargos": json.loads(r.read().decode("utf-8"))})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc), "encargos": []})
            return

        self.send_error(404)

    def do_POST(self):
        ruta = self.path.split("?")[0]

        if ruta in ("/api/levantar", "/api/detener"):
            if _tarea_en_curso["activa"]:
                self._json({"ok": False, "error": "Ya hay una operación en curso."}, 409)
                return
            if ruta == "/api/levantar":
                _lanzar_en_segundo_plano("levantar", ["docker", "compose", "up", "-d", "--build"])
            else:
                _lanzar_en_segundo_plano("detener", ["docker", "compose", "down"])
            self._json({"ok": True})
            return

        if ruta == "/api/encargo":
            datos = self._cuerpo_json()
            tipo = str(datos.get("type") or "").strip()
            prompt = str(datos.get("prompt") or "").strip()

            if tipo not in {"DIRECTOR", "RESEARCH", "ANALYSIS", "PRESENTATION", "WEBSITE"}:
                self._json({"ok": False, "error": "Tipo de encargo no válido."}, 400)
                return
            if not prompt:
                self._json({"ok": False, "error": "Escribe qué quieres encargar."}, 400)
                return

            payload = json.dumps({"type": tipo, "prompt": prompt}).encode("utf-8")
            req = urllib.request.Request(
                f"{API_MAESTRO}/api/tasks",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    self._json({"ok": True, "tarea": json.loads(r.read().decode("utf-8"))})
            except urllib.error.HTTPError as exc:
                self._json({"ok": False, "error": f"La API respondió {exc.code}."}, 502)
            except Exception:  # noqa: BLE001
                self._json(
                    {"ok": False, "error": "La API de Maestro no responde. Levanta el sistema primero."},
                    502,
                )
            return

        self.send_error(404)


def main():
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), partial(Handler))
    url = f"http://127.0.0.1:{PUERTO}"
    print(f"Panel de Maestro en {url}")
    print(f"Proyecto: {PROYECTO_DIR}")
    print("Ctrl+C para detener.")
    if "--abrir" in sys.argv:
        threading.Timer(1.0, lambda: __import__("webbrowser").open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel detenido.")


if __name__ == "__main__":
    main()
