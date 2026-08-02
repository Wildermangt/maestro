# -*- coding: utf-8 -*-
"""
Diagnóstico previo: comprueba que todo esté listo ANTES de lanzar nada.

El problema que resuelve: hasta ahora te enterabas de que faltaba una clave
o de que Docker estaba caído cuando la tarea ya había fallado — a veces
después de gastar cuota. Esto lo dice en segundos y sin gastar nada.

    python panel/doctor.py

Nunca imprime el valor de una clave: solo si está o no, y su longitud.
"""
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENV = RAIZ / ".env"

OK, AVISO, ERROR = "[ OK ]", "[AVISO]", "[FALLA]"
_problemas: list[str] = []
_avisos: list[str] = []


def linea(marca: str, texto: str, detalle: str = "") -> None:
    print(f"  {marca} {texto}" + (f" — {detalle}" if detalle else ""))
    if marca == ERROR:
        _problemas.append(texto)
    elif marca == AVISO:
        _avisos.append(texto)


def leer_env() -> dict:
    if not ENV.exists():
        return {}
    valores = {}
    for l in ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            k, v = l.split("=", 1)
            valores[k.strip()] = v.strip()
    return valores


def correr(args, timeout=20):
    try:
        p = subprocess.run(args, cwd=str(RAIZ), capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return False, "comando no encontrado"
    except subprocess.TimeoutExpired:
        return False, "tiempo agotado"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def puerto_abierto(host: str, puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)
        return s.connect_ex((host, puerto)) == 0


def http_ok(url: str, timeout=5) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code < 500, f"HTTP {e.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:60]


# ---------------------------------------------------------------- secciones

def revisar_requisitos():
    print("\nRequisitos")
    print(f"  Python {sys.version.split()[0]}")

    ok, salida = correr(["docker", "version", "--format", "{{.Server.Version}}"], 15)
    if ok:
        linea(OK, "Docker", f"motor {salida.strip()}")
    elif "no encontrado" in salida:
        linea(ERROR, "Docker no está instalado o no está en el PATH")
    else:
        linea(ERROR, "Docker instalado pero el motor no responde", "abre Docker Desktop")


def revisar_configuracion(env: dict):
    print("\nConfiguración")
    if not ENV.exists():
        linea(ERROR, "Falta el archivo .env", "copia .env.example a .env")
        return

    proveedor = (env.get("LLM_PROVIDER") or "claude").lower()
    clave_llm = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}.get(proveedor)

    if clave_llm is None:
        linea(ERROR, f"LLM_PROVIDER='{proveedor}' no es válido", "usa claude, openai o gemini")
    elif env.get(clave_llm):
        linea(OK, f"Proveedor de LLM: {proveedor}", f"{clave_llm} presente ({len(env[clave_llm])} caracteres)")
    else:
        linea(ERROR, f"LLM_PROVIDER='{proveedor}' pero falta {clave_llm}",
              "los agentes fallarán al arrancar")

    modo = (env.get("SEARCH_PROVIDER") or "auto").lower()
    nativa = proveedor in ("claude", "gemini") and modo in ("auto", "native")
    if nativa:
        linea(OK, "Búsqueda web nativa del proveedor", "no necesitas Tavily ni Firecrawl")
    else:
        for clave, nombre in (("TAVILY_API_KEY", "Tavily (búsqueda)"),
                              ("FIRECRAWL_API_KEY", "Firecrawl (lectura de páginas)")):
            if env.get(clave):
                linea(OK, nombre)
            else:
                linea(ERROR, f"Falta {clave}", f"requerida con SEARCH_PROVIDER={modo}")

    if (env.get("REQUIRE_PLAN_APPROVAL") or "").lower() in ("1", "true", "yes"):
        linea(OK, "Aprobación de plan activada", "las tareas esperarán tu visto bueno")

    tope = env.get("MAX_LLM_CALLS_PER_TASK")
    if tope:
        linea(OK, "Tope de llamadas por tarea", tope)
    else:
        linea(AVISO, "Sin MAX_LLM_CALLS_PER_TASK", "se usa el default 40; bájalo si tu cuota es pequeña")


def revisar_servicios():
    print("\nServicios")
    for nombre, host, puerto in (("PostgreSQL", "127.0.0.1", 5432),
                                 ("Redis", "127.0.0.1", 6379),
                                 ("API (backend)", "127.0.0.1", 4000)):
        if puerto_abierto(host, puerto):
            linea(OK, nombre, f"puerto {puerto}")
        else:
            linea(ERROR, f"{nombre} no responde", f"puerto {puerto} cerrado — ¿levantaste el sistema?")

    if puerto_abierto("127.0.0.1", 6333):
        linea(OK, "Qdrant (caché semántico)")
    else:
        linea(AVISO, "Qdrant apagado", "opcional; sin él no se reutilizan investigaciones previas")

    if puerto_abierto("127.0.0.1", 4000):
        ok, detalle = http_ok("http://127.0.0.1:4000/api/tasks")
        linea(OK if ok else ERROR, "API responde peticiones", detalle)
        ok, detalle = http_ok("http://127.0.0.1:4000/api/memory/entities")
        linea(OK if ok else ERROR, "Memoria disponible", detalle)


def revisar_carpetas():
    print("\nCarpetas")
    for ruta, nombre, obligatoria in ((RAIZ / "ingest", "ingest/ (archivos de entrada)", False),
                                      (RAIZ / ".gitignore", ".gitignore (protege tus claves)", True)):
        if ruta.exists():
            linea(OK, nombre)
        else:
            linea(ERROR if obligatoria else AVISO, f"Falta {nombre}")


def main() -> int:
    print("=" * 62)
    print("  Diagnóstico de Maestro")
    print("=" * 62)

    env = leer_env()
    revisar_requisitos()
    revisar_configuracion(env)
    revisar_servicios()
    revisar_carpetas()

    print("\n" + "=" * 62)
    if _problemas:
        print(f"  {len(_problemas)} problema(s) que impiden funcionar:")
        for p in _problemas:
            print(f"    - {p}")
        if _avisos:
            print(f"  {len(_avisos)} aviso(s) que no bloquean.")
        print("=" * 62)
        return 1

    if _avisos:
        print(f"  Todo lo esencial está listo. {len(_avisos)} aviso(s) menor(es):")
        for a in _avisos:
            print(f"    - {a}")
    else:
        print("  Todo listo. Puedes lanzar tareas.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
