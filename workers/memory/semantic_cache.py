"""
Caché semántico de investigaciones previas, usando Qdrant.

Antes de gastar créditos de Tavily/Firecrawl y tokens de Claude,
el Investigador busca aquí si ya existe una investigación con un
prompt semánticamente similar. Si la similitud supera el umbral,
reutiliza ese resultado en vez de investigar de nuevo.

Diseño deliberadamente simple para el Sprint 2: una sola colección,
embeddings del prompt original, sin expiración por TTL todavía
(eso es una mejora natural de Sprint 5 — invalidar caché por edad
es importante para temas que cambian rápido, como mercados).
"""
import hashlib
import logging
import os
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

logger = logging.getLogger("memory.semantic_cache")

COLLECTION_NAME = "research_cache"
SIMILARITY_THRESHOLD = 0.92  # alto a propósito: prefiere investigar de más
                              # a devolver un resultado que no responde
                              # realmente la pregunta del usuario.
EMBEDDING_DIM = 1536  # dimensión de text-embedding-3-small (OpenAI)


class SemanticCache:
    def __init__(self, url: str | None = None, embed_fn=None):
        self.client = QdrantClient(url=url or os.getenv("QDRANT_URL", "http://qdrant:6333"))
        self._embed_fn = embed_fn or self._default_embed
        self._ensure_collection()

    def _ensure_collection(self):
        existing = [c.name for c in self.client.get_collections().collections]
        if COLLECTION_NAME not in existing:
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
            )
            logger.info(f"Colección Qdrant '{COLLECTION_NAME}' creada")

    def _default_embed(self, text: str) -> list[float]:
        """
        Embedding real vía OpenAI. Si no hay OPENAI_API_KEY configurada,
        usar un embedding determinístico de respaldo (hash) para que el
        sistema siga funcionando sin caché semántico real, en vez de caer.
        """
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.warning(
                "OPENAI_API_KEY no configurada — usando embedding hash de respaldo. "
                "El caché semántico real solo encuentra duplicados EXACTOS así."
            )
            return self._hash_embed(text)

        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.embeddings.create(model="text-embedding-3-small", input=text)
        return response.data[0].embedding

    @staticmethod
    def _hash_embed(text: str) -> list[float]:
        """Fallback no-semántico: mismo texto -> mismo vector. Sirve para
        no romper el flujo en desarrollo sin key de OpenAI, pero NO agrupa
        prompts parecidos como lo haría un embedding real."""
        digest = hashlib.sha256(text.lower().strip().encode()).digest()
        # Repetir los bytes del hash hasta llenar la dimensión esperada
        repeated = (digest * (EMBEDDING_DIM // len(digest) + 1))[:EMBEDDING_DIM]
        return [b / 255.0 for b in repeated]

    def find_similar(self, prompt: str) -> dict | None:
        vector = self._embed_fn(prompt)
        hits = self.client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=1,
        ).points

        if not hits:
            return None

        best = hits[0]
        if best.score < SIMILARITY_THRESHOLD:
            logger.info(f"Mejor match en caché tiene score={best.score:.3f}, bajo el umbral")
            return None

        logger.info(f"Cache HIT para prompt (score={best.score:.3f})")
        return best.payload

    def store(self, prompt: str, result: dict):
        vector = self._embed_fn(prompt)
        self.client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload={"prompt": prompt, **result},
                )
            ],
        )
        logger.info("Resultado guardado en caché semántico")
