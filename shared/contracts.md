# Contrato de Comunicación: NestJS ↔ Worker Python

La cola BullMQ (`tasks-queue`) es el límite entre TypeScript y Python.
Redis almacena JSON puro, así que el contrato es este shape, replicado
manualmente en ambos lados (TypeScript con tipos, Python con Pydantic).

## Job que NestJS encola (BullMQ → Redis)

```json
{
  "taskId": "uuid-v4",
  "type": "DIRECTOR" | "RESEARCH" | "ANALYSIS" | "PRESENTATION" | "WEBSITE",
  "prompt": "string del usuario",
  "userId": "uuid-v4",
  "metadata": {}
}
```

## Resultado que el Worker devuelve (Redis → NestJS)

El worker escribe el resultado en una clave Redis `result:{taskId}` y publica
en el canal pub/sub `task-completed` para que NestJS reaccione sin polling.

```json
{
  "taskId": "uuid-v4",
  "status": "COMPLETED" | "FAILED" | "PARTIAL",
  "result": {
    "summary": "string",
    "data": {}
  },
  "artifacts": [
    {
      "type": "pptx" | "website",
      "filename": "string, ej: presentacion.pptx",
      "relativePath": "string relativo a /app/artifacts, ej: a1b2c3/presentacion.pptx"
    }
  ],
  "error": null,
  "completedAt": "ISO-8601"
}
```

### Artefactos de archivo (Sprint 4+)

Cuando un agente produce un archivo descargable (PPTX del Presentador,
código del sitio del Diseñador Web), el worker lo escribe en
`/app/artifacts/{taskId}/{filename}` — un volumen Docker compartido
entre `worker` y `backend` (ver docker-compose.yml). El worker NUNCA
escribe directamente en Postgres; en vez de eso, describe el archivo
en el array `artifacts` de arriba, y NestJS —al procesar `task-completed`—
crea la fila `Artifact` correspondiente con `url` apuntando al endpoint
`GET /api/tasks/:taskId/artifacts/:artifactId/download`.

Esto mantiene el límite de responsabilidad limpio: Python nunca toca
Postgres directamente (evita duplicar lógica de ORM en dos lenguajes),
y NestJS es la única fuente de verdad sobre qué artefactos existen.

## Por qué este diseño

- **Redis pub/sub en vez de que el worker llame de vuelta a NestJS por HTTP**:
  evita acoplar el worker a la disponibilidad/red del backend; NestJS simplemente
  escucha el canal cuando quiere.
- **JSON plano**: ni TypeScript ni Python imponen su runtime al otro lado.
  La validación de forma ocurre en cada lado con sus propias herramientas
  (Prisma/class-validator en Nest, Pydantic en Python).
- A medida que crezcan los agentes (Sprint 2+), el campo `metadata` y `result.data`
  son donde va la complejidad nueva — el contrato base no debería cambiar.
