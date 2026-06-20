# Imagen mínima del sandbox de ejecución del Agente Analista.
#
# Deliberadamente NO incluye pip, curl, ni herramientas de red —
# cuanto menos tenga esta imagen, menos superficie de ataque si
# alguna vez el aislamiento de red/privilegios falla en otra capa.
#
# Se construye una sola vez (ver README) y luego cada ejecución del
# Analista lanza un contenedor efímero a partir de esta imagen.
FROM python:3.12-slim

RUN pip install --no-cache-dir --break-system-packages pandas==2.2.3 \
    && rm -rf /root/.cache/pip

# Usuario no-root por defecto en tiempo de ejecución (code_executor.py
# además fuerza user="nobody" al lanzar el contenedor, esto es una
# capa adicional de defensa).
RUN useradd -m -u 1000 sandboxuser
USER sandboxuser

WORKDIR /tmp
