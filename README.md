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

### Sprint 5 — Producción y Escalado ✅ implementado (alcance acordado)

De la lista original del documento, este sprint cubrió las tres piezas
que se priorizaron: **documentación de API**, **observabilidad
(OpenTelemetry)**, y **configuración de deploy a Railway**. Tests E2E
y rate limiting quedaron fuera del alcance por decisión explícita —
ver "Lo que queda fuera" abajo.

**1. Swagger / OpenAPI.** `GET /api-docs` sirve la UI interactiva.
Los DTOs y el controller de `tasks` están decorados con `@ApiProperty`/
`@ApiOperation`/`@ApiResponse`. Ver `backend/src/main.ts`.

**2. OpenTelemetry, con trazabilidad distribuida real.** Ambos lados
—NestJS y el worker Python— están instrumentados, y lo más importante:
**el contexto de trace se propaga entre ellos**, aunque no se comunican
por HTTP sino por BullMQ/Redis (donde la auto-instrumentación no
conecta nada por sí sola). NestJS inyecta el `traceparent` W3C en el
payload del job (`tasks.service.ts`, función `propagation.inject`); el
worker lo extrae y lo usa como contexto padre de su propio span
(`workers/tracing.py`, función `extract_context`). Esto se probó
explícitamente: un `traceparent` simulado del lado Node produce un
span en Python con el mismo `trace_id`, no uno desconectado.

Exportador actual: **consola** (`ConsoleSpanExporter` en ambos lados),
por decisión explícita — no hay colector externo todavía. Cambiar a
Jaeger/Grafana/Honeycomb más adelante es solo cambiar el exportador,
la instrumentación no se toca.

**3. Configuración de deploy a Railway.** `railway.json` en `backend/`,
`workers/` y `frontend/`, más una guía completa en `DEPLOY_RAILWAY.md`
con el proceso paso a paso. Dos decisiones importantes:

- **Postgres y Redis**: plugins gestionados de Railway (backups
  automáticos), en vez de los contenedores propios de desarrollo local.
- **Qdrant**: Railway no tiene plugin gestionado, así que en producción
  se usa **Qdrant Cloud** (tier gratuito) — mismo cliente, solo cambia
  `QDRANT_URL`/`QDRANT_API_KEY`.

⚠️ **Limitación de plataforma aceptada explícitamente**: Railway no
expone el socket Docker del host, así que el sandbox del Analista NO
puede lanzar contenedores efímeros ahí — usa el fallback de subprocess
del Sprint 3 (ya construido y probado, no es una ruta nueva sin
verificar). Está documentado en `DEPLOY_RAILWAY.md`.

### Bug/inconsistencia encontrada y corregida en este sprint

`code_executor.py` forzaba `user="nobody"` al lanzar el contenedor
sandbox, lo cual sobreescribía el `USER sandboxuser` definido en
`docker/sandbox/Dockerfile` — una inconsistencia entre un usuario sin
`HOME` válido en esa imagen y la configuración ya probada del
Dockerfile. Se quitó la sobreescritura.

### Lo que queda fuera de los 5 sprints originales

No estaban en el plan original, pero quedaron como deuda técnica
explícita a lo largo del proyecto, documentada en el código donde
corresponde:

- Tests automatizados de integración/E2E y rate limiting por usuario
  (no priorizados para este sprint — agregarlos después es
  relativamente rápido una vez que el resto está en pie).
- Migrar el storage de artefactos de volumen local a MinIO/S3 real
  (el contrato ya está diseñado para que ese cambio no toque el
  frontend — ver `shared/contracts.md`).
- Agente QA (mencionado en el documento original, nunca implementado).
- Scheduler de tareas recurrentes con Celery Beat (mencionado en el
  documento original, nunca implementado).
- Repartir subtareas del Director entre múltiples workers en paralelo
  (hoy todo el plan de un Director corre en el worker que tomó ese
  job — ver limitación documentada en `agents/director.py`).

## Sprint 6 — Canales externos: Telegram, Gmail, Outlook

Más allá de los 5 sprints originales: el sistema ahora puede crear
tareas y notificar resultados desde tres canales adicionales al
dashboard web, todos siguiendo el mismo patrón de diseño:

- **Nunca encolan directo en Redis** — crean tareas llamando a
  `POST /api/tasks` del backend, igual que el frontend. NestJS sigue
  siendo la única fuente de verdad.
- **Se degradan con gracia** — sin las credenciales de un canal, ese
  canal simplemente no arranca (queda en `None`), y el resto del
  worker sigue funcionando con normalidad.
- **Corren en hilos separados** (`threading`) del loop principal de
  BullMQ, porque hacen long-polling/polling bloqueante.

### Telegram (`workers/channels/telegram.py`)

Bot restringido a un único `TELEGRAM_CHAT_ID` — cualquier otro chat
es ignorado en silencio (no se le confirma que el bot existe). Long
polling, sin necesidad de exponer el backend a internet.

### Gmail y Outlook (`workers/channels/gmail.py`, `outlook.py`)

Lectura por polling (cada `GMAIL_POLL_INTERVAL_SECONDS` /
`OUTLOOK_POLL_INTERVAL_SECONDS`, default 60s) + envío, vía OAuth2.

⚠️ **Decisión de seguridad obligatoria**: ambos canales exigen una
lista de remitentes permitidos (`GMAIL_ALLOWED_SENDERS` /
`OUTLOOK_ALLOWED_SENDERS`) — sin ella, el canal se niega a arrancar.
Sin este filtro, cualquiera que conociera tu dirección de correo
podría crear tareas a tu costo (tokens de LLM, llamadas a Tavily/
Firecrawl) simplemente escribiéndote un correo.

**Por qué OAuth2 y no contraseñas de aplicación**: Google las está
retirando progresivamente incluso para cuentas personales con 2FA
correctamente configurada — en la práctica, dejaron de estar
disponibles para muchas cuentas sin un patrón claro y documentado de
cuándo. OAuth2 es el camino que ambos proveedores garantizan que sigue
funcionando.

**Cómo obtener las credenciales** — ambos requieren un script de
autorización que se corre **una sola vez, localmente, fuera de
Docker** (no tiene sentido abrir un navegador real dentro de un
contenedor):

```bash
cd workers
pip install google-auth-oauthlib --break-system-packages
python channels/oauth_scripts/gmail_authorize.py /ruta/al/client_secret.json

pip install msal --break-system-packages
python channels/oauth_scripts/outlook_authorize.py <application-client-id>
```

Cada script imprime las variables exactas a copiar al `.env`. Ver los
comentarios extensos en `.env.example` (sección "Sprint 6") para los
pasos previos completos en Google Cloud Console / Azure Portal.

### Limitación conocida y documentada: rotación de refresh token en Outlook

Microsoft puede rotar el refresh token en cualquier uso. El canal lo
adopta en memoria para la sesión actual del contenedor, pero **no lo
persiste de vuelta al `.env`** — si el contenedor se reinicia después
de una rotación y antes de que actualices el `.env` a mano, tendrás
que re-autorizar con el script. Aceptable para un solo usuario; si
esto se lleva a producción con más usuarios, vale la pena resolverlo
escribiendo el token rotado a un almacén persistente.

### Bug encontrado y corregido en este sprint

Las variables opcionales `GMAIL_POLL_INTERVAL_SECONDS` y
`OUTLOOK_POLL_INTERVAL_SECONDS`, al pasarse desde `docker-compose.yml`
con la sintaxis `${VAR:-}` (vacío si no está en `.env`), llegaban como
cadena vacía `""` al contenedor — no como variable ausente. El patrón
`int(os.getenv("X", "60"))` solo usa el default cuando la variable
**no existe**, no cuando existe pero está vacía, así que esto habría
crasheado con `ValueError` en cualquier despliegue donde el usuario no
llenara esas variables opcionales. Corregido a
`int(os.getenv("X") or 60)`, que cubre ambos casos — verificado con
los tres escenarios (ausente, vacía, con valor).



