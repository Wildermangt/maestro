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

### Siguiente paso (Sprint 3) ✅ implementado

El Agente Director ahora usa LangGraph de verdad: descompone el objetivo
del usuario en subtareas (`RESEARCH` y/o `ANALYSIS`), las ejecuta **en
paralelo cuando no dependen entre sí** (oleadas respetando `dependsOn`),
y consolida un resumen ejecutivo final con Claude. Ver `workers/agents/director.py`.

Se agregó también el **Agente Analista** (`workers/agents/analyst.py`):
genera código Python con Claude, lo ejecuta en un sandbox aislado
(`workers/tools/code_executor.py` — subprocess con timeout de 30s y
sin acceso a las variables de entorno del worker, así el código generado
nunca puede leer tus API keys), y si falla, le pide a Claude que lo
corrija una vez antes de rendirse.

### Árbol de subtareas en tiempo real

Cada vez que una subtarea cambia de estado (PENDING → RUNNING →
COMPLETED/FAILED), el Director publica un evento en el canal Redis
`task-progress` (separado de `task-completed`). NestJS lo reenvía por
WebSocket (`task:subtask-progress`) y el frontend pinta el árbol en
vivo — sin esperar a que el Director termine todo el plan.

### Probar el Director

```bash
docker compose up --build
```

```bash
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "DIRECTOR", "prompt": "Investiga el mercado cripto en Colombia y calcula proyecciones de adopción para 2027"}'
```

En el dashboard verás aparecer las subtareas (Investigador, Analista)
con su estado en vivo, y al final el resumen consolidado.

### Decisiones de diseño de este sprint

- **Los sub-agentes se invocan in-process, no se vuelven a encolar en
  BullMQ.** Encolar de vuelta crearía riesgo de deadlock con un solo
  worker (el worker esperando su propio job). Si más adelante se escalan
  múltiples workers y se quiere repartir subtareas entre ellos, esto
  hay que revisarlo — está documentado en `director.py`.
- **El sandbox del Analista es un subprocess aislado, no Docker real
  todavía.** Cumple la regla de seguridad central (sin acceso a env vars,
  timeout estricto), pero no aísla a nivel de sistema de archivos/red
  como un contenedor. Mover a Docker real es trabajo de Sprint 4.
- **Paralelismo con ThreadPoolExecutor**, no asyncio. Los agentes hacen
  llamadas HTTP bloqueantes (requests a Tavily/Firecrawl/Anthropic), así
  que hilos son más simples aquí que reescribir todo a async/await.

### Siguiente paso (Sprint 4)

Agentes Presentador (python-pptx) y Diseñador Web (deploy a Vercel),
y mover el sandbox del Analista a un contenedor Docker efímero real.


