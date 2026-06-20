# Desplegar a Railway

Railway no tiene un equivalente directo a `docker-compose up` para un
monorepo — cada servicio se crea por separado en el dashboard y se
conecta a los demás vía variables de entorno. Esta guía asume que ya
tienes una cuenta y el [Railway CLI](https://docs.railway.com/guides/cli)
instalado (`npm i -g @railway/cli`), aunque todo se puede hacer también
desde el dashboard web.

## Diferencias con desarrollo local

| Pieza | Local (docker-compose) | Railway |
|---|---|---|
| Postgres | Contenedor propio | Plugin gestionado de Railway |
| Redis | Contenedor propio | Plugin gestionado de Railway |
| Qdrant | Contenedor propio | **Qdrant Cloud** (Railway no tiene plugin) |
| Sandbox del Analista | Docker real (contenedores efímeros) | **Fallback de subprocess** (Railway no expone el socket Docker del host) |

La limitación del sandbox es importante: en Railway, el código que
genera el Analista corre con el mismo aislamiento que en el Sprint 3
(subprocess sin red ni env vars, pero sin el aislamiento de
filesystem/red a nivel de sistema operativo que da un contenedor real).
Es una limitación de la plataforma, no algo que se pueda resolver
sin cambiar de proveedor o usar un servicio de sandboxing externo.

## Paso 1: Crear el proyecto y los plugins gestionados

```bash
railway login
railway init
```

Desde el dashboard del proyecto, añade:
- **PostgreSQL** (botón "+ New" → Database → PostgreSQL)
- **Redis** (botón "+ New" → Database → Redis)

Railway genera automáticamente `DATABASE_URL` y `REDIS_URL` para estos
plugins. **No las copies manualmente** — en el paso 3 se referencian
con la sintaxis de variables de Railway.

## Paso 2: Qdrant Cloud

Railway no tiene plugin de Qdrant. Crea un cluster gratuito en
[cloud.qdrant.io](https://cloud.qdrant.io) y copia:
- La URL del cluster (algo como `https://xxxxx.cloud.qdrant.io`)
- El API key generado

Estos van directo en las variables del servicio `worker` (paso 4), no
hay nada que crear en Railway para esto.

## Paso 3: Servicio `backend`

```bash
cd backend
railway link   # selecciona el proyecto creado en el paso 1
railway up
```

En el dashboard, configura el **Root Directory** de este servicio
como `backend` (si desplegaste desde la raíz del repo en vez de `cd backend`
antes de `railway up`). Railway detecta `backend/railway.json` y usa el
Dockerfile automáticamente.

Variables de entorno del servicio `backend` (dashboard → Variables):

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
FRONTEND_URL=https://<tu-dominio-frontend>.up.railway.app
PORT=4000
ARTIFACTS_DIR=/app/artifacts
RUN_SEED=true
```

La sintaxis `${{Postgres.DATABASE_URL}}` referencia la variable que el
plugin de Postgres ya expone — Railway la resuelve automáticamente,
no escribas la URL a mano.

⚠️ `RUN_SEED=true` crea el usuario de prueba con ID fijo, igual que en
desarrollo local — sigue siendo necesario porque no hay Firebase Auth
implementado todavía. El día que haya autenticación real, cambia esto
a `false` y crea usuarios reales vía el flujo de auth.

### Volumen de artefactos en `backend`

Railway soporta volúmenes persistentes por servicio. Desde el dashboard:
Service → Settings → Volumes → "+ New Volume", monta en `/app/artifacts`.
**Este volumen debe ser el MISMO al que se monta en `worker`** — en
Railway, los volúmenes no se comparten entre servicios distintos como
en docker-compose. Si necesitas que ambos servicios vean los mismos
archivos, la opción real es migrar a un storage compartido externo
(S3/MinIO, tal como ya estaba planeado para Sprint 5+ en el documento
original) en vez de un volumen local — esto NO está resuelto en esta
guía, queda como siguiente paso si los artefactos son prioritarios en
producción real.

## Paso 4: Servicio `worker`

```bash
cd ../workers
railway up
```

Root Directory: `workers`.

Variables de entorno:

```
REDIS_URL=${{Redis.REDIS_URL}}
QUEUE_NAME=tasks-queue
QDRANT_URL=https://xxxxx.cloud.qdrant.io
QDRANT_API_KEY=<tu-api-key-de-qdrant-cloud>
TAVILY_API_KEY=<tu-key>
FIRECRAWL_API_KEY=<tu-key>
ANTHROPIC_API_KEY=<tu-key>
OPENAI_API_KEY=<tu-key>          # opcional, embeddings del caché
VERCEL_TOKEN=<tu-token>
ARTIFACTS_DIR=/app/artifacts
```

Mismo volumen `/app/artifacts` que en `backend` (ver limitación arriba).

## Paso 5: Servicio `frontend`

```bash
cd ../frontend
railway up
```

Root Directory: `frontend`. Variables:

```
NEXT_PUBLIC_API_URL=https://<tu-dominio-backend>.up.railway.app
```

Como `NEXT_PUBLIC_API_URL` se hornea en build-time (ver
`frontend/Dockerfile`, sección `ARG`/`ENV`), Railway necesita esta
variable disponible durante el build, no solo en runtime — en Railway
esto funciona automáticamente porque las variables del servicio están
disponibles en ambas fases.

## Paso 6: Dominios públicos

Cada servicio necesita un dominio para ser accesible. Dashboard →
Service → Settings → Networking → "Generate Domain" (o conecta un
dominio propio). Hazlo para `backend` y `frontend` — `worker` no
necesita dominio público, solo habla con Redis/Qdrant/APIs externas.

Tras generar los dominios, actualiza `FRONTEND_URL` en `backend` y
`NEXT_PUBLIC_API_URL` en `frontend` con las URLs reales (Railway no
las conoce de antemano).

## Verificar el deploy

```bash
curl https://<tu-dominio-backend>.up.railway.app/api/health
```

Debería responder `{"status":"ok","service":"maestro-backend"}`.

```bash
curl https://<tu-dominio-backend>.up.railway.app/api-docs
```

Debería servir la UI de Swagger.

## Observabilidad en Railway

Los logs de OpenTelemetry (exportador consola, ver
`backend/src/telemetry/tracing.ts` y `workers/tracing.py`) aparecen
mezclados con el resto de logs de la aplicación en el dashboard de
Railway (Service → Logs/Observability). Railway no tiene un colector
OTLP nativo — si más adelante quieres trazas estructuradas y
consultables (no solo texto en logs), el cambio es apuntar
`traceExporter`/`ConsoleSpanExporter` a un colector real (Honeycomb,
Grafana Cloud, etc.) vía `OTLPTraceExporter`, sin tocar la
instrumentación.
