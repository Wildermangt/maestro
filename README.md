# Maestro

Sistema distribuido que recibe una tarea en lenguaje natural, la descompone en
subtareas, las reparte entre agentes especializados y devuelve un resultado —
que puede ser una respuesta, una presentación, una hoja de cálculo o un sitio web
desplegado.

**7 servicios orquestados con Docker Compose · 11 agentes · 6.582 líneas de Python
y 1.590 de TypeScript.**

> 📓 Las decisiones de diseño de cada etapa y los errores encontrados al probar
> están en **[`BITACORA-SPRINTS.md`](BITACORA-SPRINTS.md)**, escrita mientras el
> proyecto se construía.

---

## Cómo está montado

```
Next.js  ──POST /api/tasks──▶  NestJS  ──BullMQ──▶  Redis
   ▲                             │                    │
   └────── WebSocket ────────────┘                    ▼
                                              Worker (Python)
                                                     │
                            ┌────────────────────────┼────────────────────┐
                            ▼                        ▼                    ▼
                        Postgres                  Qdrant          Contenedor efímero
                     (estado, memoria)       (caché semántico)     (ejecuta código)
```

| Servicio | Papel |
|---|---|
| `frontend` | Next.js. Envía la tarea y recibe el árbol de subtareas en tiempo real |
| `backend` | NestJS. API REST, WebSocket, Prisma sobre PostgreSQL |
| `worker` | Python. Desencola de Redis y ejecuta los agentes |
| `postgres` | Estado de las tareas y memoria de entidades |
| `redis` | Cola de trabajo (BullMQ) |
| `qdrant` | Caché semántico. **Opcional**: sin él el sistema funciona, solo deja de reutilizar investigaciones previas |
| `sandbox-image` | No corre como servicio: construye la imagen desde la que se lanzan contenedores efímeros |

## Los agentes

El **Director** no ejecuta: descompone y delega. Los demás hacen el trabajo.

| Agente | Qué hace |
|---|---|
| `director` | Descompone la tarea en subtareas y las reparte |
| `researcher` | Busca en la web y sintetiza |
| `analyst` | Ejecuta código para analizar datos |
| `extractor` | Extrae datos estructurados de documentos |
| `enricher` | Completa información faltante |
| `verifier` | Contrasta resultados antes de darlos por buenos |
| `writer` | Redacta el texto final |
| `presenter` | Genera presentaciones |
| `spreadsheet` | Genera hojas de cálculo |
| `designer` | Genera sitios web |
| `ingest` | Lee archivos de una carpeta autorizada |

El árbol de subtareas se transmite al navegador **mientras se construye**, no al
terminar.

## Tres decisiones que vale la pena explicar

**El código generado se ejecuta en un contenedor sin red.** Un agente que escribe
y ejecuta código es un riesgo evidente. Cada ejecución lanza un contenedor de usar
y tirar con `network_disabled=True` (no puede exfiltrar nada ni llamar APIs),
`cap_drop=["ALL"]`, sistema de archivos raíz de solo lectura con `tmpfs` en `/tmp`,
límite de memoria, tiempo máximo y borrado automático al terminar.

**La ingesta resuelve y verifica cada ruta.** El agente de ingesta lee de una sola
carpeta, y toda ruta se resuelve y se comprueba que quede dentro antes de abrirla
— si no, un nombre con `../` permitiría leer cualquier archivo del contenedor.

**Las dependencias entre servicios son condicionales, no de orden.** El backend no
arranca cuando Postgres existe, sino cuando responde: `depends_on` con
`condition: service_healthy` y `healthcheck` en Postgres y Redis. El worker además
espera a que la imagen del sandbox termine de construirse
(`service_completed_successfully`).

Qdrant es el caso contrario a propósito: está en un perfil de Compose y **no** se
declara como dependencia, porque Compose rechaza depender de un servicio que puede
no existir. El agente investigador envuelve su uso en `try/except` y sigue sin él.

## Levantar el sistema

```bash
cp .env.example .env     # y completa las claves que vayas a usar
docker compose up --build
```

- Frontend: http://localhost:3000
- Backend: http://localhost:4000

Para incluir el caché semántico: `docker compose --profile completo up --build`

El proveedor de LLM se elige con `LLM_PROVIDER` (`claude`, `openai` o `gemini`) y
`workers/utils/llm_factory.py` resuelve el cliente. Los topes de consumo por tarea
—llamadas al modelo, subtareas, profundidad de descomposición, tiempo— se
controlan por variables de entorno y están en `workers/config.py`.

## Canales de entrada

Además del navegador, acepta tareas por **Telegram**, **Gmail** y **Outlook**, con
lista de remitentes autorizados (`workers/channels/`).

## Nota sobre el historial

Este repositorio se publicó en septiembre de 2026, pero el proyecto se construyó
entre junio y agosto. El historial se reconstruyó a partir de los **snapshots
guardados al cerrar cada sprint**, conservando sus fechas originales, para que la
progresión sea legible. No es un historial de trabajo continuo: cada commit es el
estado del proyecto al final de esa etapa.

## Qué no está en el repositorio

`.env` está excluido: contiene claves de API reales. La plantilla con todas las
variables que el sistema puede usar está en `.env.example`.
