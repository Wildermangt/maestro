# Panel de Control de Maestro

Interfaz local para levantar el sistema, ver el estado de los seis servicios y
encargar trabajo a los agentes sin escribir un solo comando.

![Ruta de un encargo](#) <!-- reemplaza por una captura cuando lo tengas corriendo -->

## Abrirlo

```powershell
.\panel\iniciar.ps1
```

O directamente:

```bash
python panel/servidor.py --abrir
```

Queda en <http://127.0.0.1:8770>.

## Qué hace

- **Estado en vivo.** Consulta `docker compose ps` cada 4 segundos y pinta el
  estado real de `frontend`, `backend`, `redis`, `worker`, `postgres` y `qdrant`.
- **Levantar y detener.** Ejecuta `docker compose up -d --build` y `docker compose down`
  en segundo plano, con la salida en vivo en la consola del panel.
- **Diagnóstico de configuración.** Detecta si falta el `.env` y si falta la clave
  del proveedor de LLM que tienes seleccionado en `LLM_PROVIDER`, que es la causa
  más común de que los agentes no arranquen.
- **Encargar trabajo.** Manda un `POST /api/tasks` a la API de Maestro y muestra
  los encargos recientes con su estado.

## Diseño

El elemento central es **«la ruta de un encargo»**: los seis servicios dibujados
en el orden real que recorre una petición —frontend → API → cola → worker → base
de datos → memoria vectorial— donde cada parada se enciende según su estado real.
Al encargar trabajo, una ficha recorre el camino.

No es decoración: es el diagrama de arquitectura del sistema, y funciona como
monitor.

## Requisitos

- **Python 3.9+** para el panel. No añade dependencias: solo librería estándar.
- **Docker Desktop** para levantar el stack. Sin él, el panel funciona igual pero
  te avisa de que el motor no responde y desactiva los botones de arranque.

## Seguridad

El servidor escucha solo en `127.0.0.1` y **nunca devuelve el valor de una
variable de entorno**: de cada clave informa únicamente si está configurada o no.
Es una herramienta de desarrollo local; no la expongas a una red.
