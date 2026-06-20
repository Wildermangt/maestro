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

### Siguiente paso (Sprint 4) ✅ implementado

Tres piezas nuevas:

**1. Sandbox del Analista migrado a Docker real.** Cada ejecución de
código generado por el LLM ahora lanza un contenedor efímero (imagen
`maestro-sandbox`, definida en `docker/sandbox/Dockerfile`): sin red,
sin privilegios, memoria limitada a 256MB, filesystem de solo lectura
excepto `/tmp`. El worker habla con el daemon Docker del HOST a través
de un socket montado (`/var/run/docker.sock`) — ver la nota de
seguridad extensa en `workers/tools/code_executor.py` antes de llevar
esto fuera de un entorno de desarrollo local. Si Docker no está
disponible, cae automáticamente al subprocess aislado del Sprint 3
(degradación con gracia, no falla el Analista).

⚠️ **No verificado con Docker real de extremo a extremo** (no estuvo
disponible en el entorno de desarrollo) — la primera vez que lo uses,
revisa los logs del worker por si hay errores de "Read-only file
system"; está documentado en el código qué ajustar si pasa.

**2. Agente Presentador** (`workers/agents/presenter.py`): Claude
estructura el contenido en slides, `python-pptx` construye el archivo
real. Probado generando un PPTX real y reabriéndolo para confirmar
que es válido.

**3. Agente Diseñador Web** (`workers/agents/designer.py`): Claude
genera un sitio estático de una sola página (HTML/CSS inline, sin
imágenes generadas — alcance acordado), y si `VERCEL_TOKEN` está
configurada, lo despliega vía la API de Vercel. Generar el sitio y
desplegarlo son capacidades independientes: si el deploy falla o no
hay token, la tarea sigue siendo `COMPLETED` con el HTML descargable.

### Artefactos descargables

Ambos agentes nuevos guardan su archivo en un volumen compartido
(`artifacts_data`, montado en `worker:/app/artifacts` y
`backend:/app/artifacts`). El worker nunca toca Postgres directamente
— describe el archivo en `TaskResult.artifacts` (ver
`shared/contracts.md`), y NestJS crea el registro `Artifact`
correspondiente al procesar `task-completed`. La descarga real pasa
por:

```
GET /api/tasks/:taskId/artifacts/:artifactId/download
```

con un guard explícito contra path traversal (la ruta resuelta debe
quedar dentro de `ARTIFACTS_ROOT`).

### Variables de entorno nuevas

```
VERCEL_TOKEN=     # https://vercel.com/account/tokens
```

### Probar el Sprint 4

```bash
docker compose up --build
```

La primera vez, esto también construye la imagen `maestro-sandbox`
(servicio `sandbox-image` en docker-compose.yml — no corre como
contenedor persistente, solo se construye y queda disponible para que
el worker lance contenedores efímeros a partir de ella).

```bash
# Presentación
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "PRESENTATION", "prompt": "Tendencias de adopción cripto en Latinoamérica"}'

# Sitio web
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "WEBSITE", "prompt": "Landing page para un fotógrafo de bodas en México"}'

# Director delegando a Investigador + Presentador
curl -X POST http://localhost:4000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "DIRECTOR", "prompt": "Investiga el mercado cripto en Colombia y crea una presentación"}'
```

El dashboard mostrará un botón de descarga para el PPTX/HTML, y si el
Diseñador Web logró desplegar, un link directo al sitio en vivo.

### Bug encontrado y corregido en este sprint

El Director no propagaba los `artifacts` de subtareas delegadas
(ej: una subtarea `PRESENTATION`) a su propio resultado final, y
usaba un `taskId` compuesto para las subtareas que habría hecho que
los archivos se guardaran en una carpeta distinta a la que el usuario
ve en el dashboard. Ambos se corrigieron — ver `agents/director.py`,
método `_run_subtask`, y se agregó una prueba específica para esto
antes de cerrar el sprint.

### Siguiente paso (Sprint 5)

Producción y escalado: tests E2E, OpenTelemetry, rate limiting,
documentación de API, deploy a Kubernetes/Railway. También sería el
momento de migrar el storage de artefactos de volumen local a
MinIO/S3 real (el contrato ya está diseñado para que ese cambio no
toque el frontend).


