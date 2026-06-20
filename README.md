# Prompt Maestro — Walking Skeleton

Sprint 1: un mensaje viaja de punta a punta.

```
Next.js (frontend) 
   → POST /api/tasks → NestJS (backend)
   → BullMQ encola en Redis
   → Worker Python desencola, procesa, devuelve "Hola Mundo"
   → NestJS recibe resultado → emite WebSocket
   → Frontend recibe notificación en tiempo real
```

## Levantar todo el sistema

```bash
docker compose up --build
```

Servicios:
- **Frontend**: http://localhost:3000
- **Backend (NestJS)**: http://localhost:4000
- **Redis**: localhost:6379
- **Postgres**: localhost:5432
- **Worker Python**: sin puerto expuesto (consume la cola)

## Probar el flujo manualmente (sin frontend)

```bash
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "DIRECTOR", "prompt": "Analiza el mercado de criptomonedas en Colombia"}'
```

Verás la tarea encolarse, el worker procesarla (logs en `docker compose logs -f worker`),
y el resultado emitido por WebSocket — visible en el dashboard de http://localhost:3000.

## Estructura

```
maestro/
├── frontend/    # Next.js 15 + WebSocket client
├── backend/     # NestJS + BullMQ + Prisma + WebSocket Gateway
├── workers/     # Python worker que consume BullMQ vía Redis
├── shared/      # Contrato de tipos (referencia, no compartido en build real)
└── docker-compose.yml
```

## Siguiente paso (Sprint 2) ✅ implementado

El Agente Investigador real ya está implementado: Tavily (búsqueda) →
Firecrawl (lectura completa de las top fuentes) → Claude (síntesis) →
Qdrant (caché semántico). Ver `workers/agents/researcher.py`.

### Variables de entorno nuevas requeridas

Copia `.env.example` a `.env` y completa:

```
TAVILY_API_KEY=        # https://app.tavily.com
FIRECRAWL_API_KEY=     # https://www.firecrawl.dev
ANTHROPIC_API_KEY=     # https://console.anthropic.com
OPENAI_API_KEY=        # opcional, solo para embeddings del caché semántico
```

⚠️ **Nunca pegues estas keys en un chat ni las commitees a git.** Van solo
en tu `.env` local, que ya está en `.gitignore`.

### Probar el Investigador

```bash
docker compose up --build
```

```bash
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "RESEARCH", "prompt": "Analiza el mercado de criptomonedas en Colombia 2026"}'
```

El resultado llega por WebSocket con `summary`, `key_findings` (lista) y
`sources` (con URLs verificables) — visible en el dashboard.

### Decisión de diseño: BullMQ desde Python

No existe cliente oficial maduro de `bullmq` para Python. El worker usa
un cliente casero (`workers/bullmq_client.py`) que habla directo con las
estructuras Redis de BullMQ. Funciona para jobs simples sin prioridad/delay.
Si en el futuro usas esas opciones al encolar desde NestJS, hay que migrar
a la librería `bullmq-python` — está documentado en ese archivo.

### Siguiente paso (Sprint 3)

Agente Director real con LangGraph: descomponer un objetivo complejo en
subtareas y delegar al Investigador (y a los agentes que falten: Analista,
Escritor, etc.) en paralelo o secuencial según dependencias.

