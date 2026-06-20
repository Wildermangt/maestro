"""
Sandbox de ejecución de código Python generado dinámicamente.

Diseño para el Sprint 4: en producción esto corre en un contenedor
Docker efímero separado (ver documento original, sección Seguridad).
Para el Sprint 3, usamos un subprocess aislado con timeout estricto
y sin acceso a variables de entorno del proceso padre — es la versión
más simple que ya cumple la regla de seguridad central: el código del
LLM nunca corre con privilegios del worker ni puede leer ANTHROPIC_API_KEY,
TAVILY_API_KEY, etc.

Limitación conocida y deliberada: subprocess no aísla a nivel de
sistema de archivos ni de red como lo haría un contenedor Docker.
No ejecutar esto con código de fuente no confiable en producción
sin moverlo al sandbox Docker real del Sprint 4.
"""
import logging
import subprocess
import sys
import tempfile

logger = logging.getLogger("tools.code_executor")

EXECUTION_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 8000


class CodeExecutionResult:
    def __init__(self, success: bool, stdout: str, stderr: str):
        self.success = success
        self.stdout = stdout[:MAX_OUTPUT_CHARS]
        self.stderr = stderr[:MAX_OUTPUT_CHARS]


class CodeExecutorTool:
    def run(self, code: str) -> CodeExecutionResult:
        """
        Ejecuta `code` como un script Python independiente.
        - Sin acceso a las variables de entorno del proceso padre
          (env={} explícito), para que un código generado por el LLM
          no pueda leer API keys del worker.
        - Timeout estricto: un loop infinito no cuelga el worker.
        """
        logger.info(f"Ejecutando código ({len(code)} chars) con timeout={EXECUTION_TIMEOUT_SECONDS}s")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            script_path = f.name

        try:
            proc = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=EXECUTION_TIMEOUT_SECONDS,
                env={},  # sandbox: sin env vars del worker (ni API keys)
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
            logger.exception("Error inesperado ejecutando código en sandbox")
            return CodeExecutionResult(success=False, stdout="", stderr=str(exc))
