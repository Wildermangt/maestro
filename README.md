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

## Siguiente paso (Sprint 2)

Sustituir el "Hola Mundo" del worker por el Agente Investigador real (CrewAI + Tavily + Firecrawl).
El contrato de mensajes ya está listo para soportar payloads más complejos — ver `shared/contracts.md`.
