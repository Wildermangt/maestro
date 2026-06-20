"""
Almacenamiento de artefactos de archivo (PPTX, sitios generados, etc.)
en el volumen compartido con el backend NestJS.

Sprint 4: filesystem local (volumen Docker). Sprint 5+: cuando se
migre a MinIO/S3 (ver documento original), solo esta clase cambia —
el contrato hacia NestJS (ver shared/contracts.md, campo `artifacts`)
no necesita tocarse, porque ya describe el archivo de forma abstracta
(type/filename/relativePath) en vez de asumir un filesystem local.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("utils.artifact_storage")

ARTIFACTS_ROOT = Path(os.getenv("ARTIFACTS_DIR", "/app/artifacts"))


class ArtifactRef:
    def __init__(self, artifact_type: str, filename: str, relative_path: str):
        self.type = artifact_type
        self.filename = filename
        self.relativePath = relative_path

    def to_dict(self) -> dict:
        return {"type": self.type, "filename": self.filename, "relativePath": self.relativePath}


def save_artifact(task_id: str, filename: str, content_bytes: bytes, artifact_type: str) -> ArtifactRef:
    """
    Guarda `content_bytes` en /app/artifacts/{task_id}/{filename} y
    devuelve la referencia que se incluye en TaskResult.artifacts.
    """
    task_dir = ARTIFACTS_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    file_path = task_dir / filename
    file_path.write_bytes(content_bytes)

    relative_path = f"{task_id}/{filename}"
    logger.info(f"Artefacto guardado: {relative_path} ({len(content_bytes)} bytes)")

    return ArtifactRef(artifact_type=artifact_type, filename=filename, relative_path=relative_path)
