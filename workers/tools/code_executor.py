"""
Sandbox de ejecución de código Python generado dinámicamente — Docker real (Sprint 4).

Cada ejecución lanza un contenedor Docker EFÍMERO y aislado:
  - Imagen mínima (python:3.12-slim + pandas preinstalado, ver
    docker/sandbox/Dockerfile).
  - Sin red (network_disabled=True): el código no puede hacer
    peticiones HTTP, ni exfiltrar datos, ni llamar APIs externas.
  - Sin acceso a las variables de entorno del worker (no se pasan).
  - Memoria limitada (256MB) y sin privilegios (non-root, --cap-drop ALL).
  - Filesystem de solo lectura excepto /tmp, para que el script no
    pueda persistir nada fuera del contenedor que se destruye al salir.
  - Timeout estricto + remoción automática del contenedor (remove=True).

⚠️ NOTA DE SEGURIDAD IMPORTANTE — cómo el worker lanza estos contenedores:
El worker no tiene Docker instalado dentro de sí mismo. En vez de eso,
se monta el socket del Docker del HOST (/var/run/docker.sock) dentro del
contenedor del worker (ver docker-compose.yml), y usamos el SDK oficial
`docker` de Python para hablar con ese daemon y pedirle que lance
contenedores HERMANOS (no anidados — esto es más simple y estable que
Docker-in-Docker real).

Esto significa que el contenedor del worker tiene la capacidad de
controlar el daemon Docker del host. Es el patrón estándar de la
industria para "un servicio que lanza sandboxes", pero es un nivel de
acceso más amplio que ejecutar subprocess directamente: si el worker
mismo llegara a verse comprometido, podría usar ese socket para hacer
más que solo correr el código del Analista. Mitigamos limitando lo que
el CONTENEDOR HIJO puede hacer (sin red, sin privilegios, memoria
limitada), pero el socket montado en el worker sigue siendo un punto
de confianza que vale la pena conocer si este proyecto sale de un
entorno de desarrollo local.

Fallback: si Docker no está disponible (el socket no está montado, o
el SDK no puede conectar), este módulo cae automáticamente al sandbox
de subprocess del Sprint 3 — degradación con gracia, igual que el
resto del sistema, en vez de tumbar al Analista por completo.

⚠️ RIESGO NO VERIFICADO EMPÍRICAMENTE: Docker no estuvo disponible en
el entorno donde se desarrolló este código, así que la combinación
read_only=True + tmpfs en /tmp + pandas no se pudo probar lanzando un
contenedor real de extremo a extremo. Es la configuración estándar
recomendada y debería funcionar (pandas no necesita escribir fuera de
/tmp en uso normal), pero la PRIMERA vez que corras el Analista con
Docker disponible, revisa los logs si ves errores de "Read-only file
system" — la solución más simple sería agregar más rutas a `tmpfs`
(ej: el HOME de sandboxuser) en vez de quitar read_only por completo.
"""
import logging
import subprocess
import sys
import tempfile

logger = logging.getLogger("tools.code_executor")

EXECUTION_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 8000
SANDBOX_IMAGE = "maestro-sandbox:latest"
MEMORY_LIMIT = "256m"


class CodeExecutionResult:
    def __init__(self, success: bool, stdout: str, stderr: str):
        self.success = success
        self.stdout = stdout[:MAX_OUTPUT_CHARS]
        self.stderr = stderr[:MAX_OUTPUT_CHARS]


class CodeExecutorTool:
    def __init__(self):
        self._docker_client = None
        self._docker_available = self._try_init_docker()

    def _try_init_docker(self) -> bool:
        try:
            import docker

            client = docker.from_env()
            client.ping()
            self._docker_client = client
            logger.info("Sandbox Docker disponible — el código se ejecutará en contenedores efímeros")
            return True
        except Exception as exc:
            logger.warning(
                f"Docker no disponible ({exc}). Usando fallback de subprocess aislado "
                "(menos seguro: sin aislamiento de red/filesystem real)."
            )
            return False

    def run(self, code: str) -> CodeExecutionResult:
        if self._docker_available:
            return self._run_in_docker(code)
        return self._run_in_subprocess(code)

    # --- Sandbox real: contenedor Docker efímero ---

    def _run_in_docker(self, code: str) -> CodeExecutionResult:
        logger.info(f"Ejecutando código ({len(code)} chars) en contenedor Docker efímero")

        try:
            container = self._docker_client.containers.run(
                image=SANDBOX_IMAGE,
                command=["python", "-c", code],
                detach=True,
                network_disabled=True,          # sin red: no puede exfiltrar ni llamar APIs
                mem_limit=MEMORY_LIMIT,
                cap_drop=["ALL"],                # sin privilegios de kernel
                security_opt=["no-new-privileges"],
                read_only=True,                  # filesystem raíz inmutable
                tmpfs={"/tmp": "size=64m"},       # único espacio escribible, en memoria
                # No se fuerza `user` aquí — se usa el USER definido en
                # docker/sandbox/Dockerfile (sandboxuser, UID 1000), que es
                # un usuario real de esa imagen con HOME y permisos conocidos.
                # Forzar "nobody" aquí lo sobreescribiría con un usuario sin
                # HOME válido en esta imagen, lo cual podría romper pandas/
                # Python al intentar escribir cachés — no se pudo verificar
                # empíricamente sin Docker disponible en el entorno de
                # desarrollo, así que se prefiere la config ya probada del
                # Dockerfile en vez de una sobreescritura sin probar.
                environment={},                   # sin env vars del worker (ni API keys)
                remove=False,                     # lo removemos manualmente tras leer logs
            )
        except Exception as exc:
            logger.exception("No se pudo lanzar el contenedor sandbox")
            return CodeExecutionResult(success=False, stdout="", stderr=f"Error lanzando sandbox Docker: {exc}")

        try:
            result = container.wait(timeout=EXECUTION_TIMEOUT_SECONDS)
            exit_code = result.get("StatusCode", 1)
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")

            # Docker mezcla stdout/stderr en un solo stream por defecto;
            # separarlos de verdad requeriría streams demux. Para el shape
            # esperado (JSON puro por stdout) esto basta: si exit_code==0
            # asumimos que `logs` es el stdout limpio del script.
            if exit_code == 0:
                return CodeExecutionResult(success=True, stdout=logs, stderr="")
            return CodeExecutionResult(success=False, stdout="", stderr=logs)

        except Exception as exc:
            logger.warning(f"Timeout o error esperando el contenedor sandbox: {exc}")
            try:
                container.kill()
            except Exception:
                pass
            return CodeExecutionResult(
                success=False,
                stdout="",
                stderr=f"Timeout: la ejecución excedió {EXECUTION_TIMEOUT_SECONDS} segundos.",
            )
        finally:
            try:
                container.remove(force=True)
            except Exception:
                logger.warning("No se pudo remover el contenedor sandbox tras la ejecución")

    # --- Fallback: subprocess aislado (Sprint 3, sin Docker disponible) ---

    def _run_in_subprocess(self, code: str) -> CodeExecutionResult:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            script_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                env={},
            )
            return CodeExecutionResult(
                success=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"Ejecución de código excedió {EXECUTION_TIMEOUT_SECONDS}s, abortada")
            return CodeExecutionResult(
                success=False,
                stdout="",
                stderr=f"Timeout: la ejecución excedió {EXECUTION_TIMEOUT_SECONDS} segundos.",
            )
        except Exception as exc:
            logger.exception("Error inesperado ejecutando código en sandbox de respaldo")
            return CodeExecutionResult(success=False, stdout="", stderr=str(exc))
