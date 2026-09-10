# Imagen slim: la superficie de ataque y el arranque en frio importan porque
# Cloud Run escala a cero entre evaluaciones.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Las dependencias van en su propia capa para que un cambio de codigo no
# invalide el cache de pip en cada build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Usuario sin privilegios: Cloud Run no lo exige, pero es higiene basica.
RUN useradd --create-home --uid 1001 agente && chown -R agente:agente /srv
USER agente

EXPOSE 8080

# Cloud Run inyecta PORT. Un solo worker: el proceso es I/O-bound (espera al
# modelo), asi que la concurrencia sale de atender esas esperas en paralelo y
# no de levantar mas procesos. El trabajo bloqueante -- el cliente sincrono de
# Anthropic -- se manda a un threadpool para no detener el event loop.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 65
