# pms-agent-demo application image (web app and alert worker; see docker/entrypoint.sh).
# Local use: docker build -t pms-agent-demo . && docker run --rm -p 8000:8000 -e APP_ENV=local pms-agent-demo
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    PORT=8000 \
    DATA_DIR=/app/var

WORKDIR /app

# Unprivileged runtime user (fixed IDs so volume permissions are predictable).
RUN groupadd --system --gid 10001 pms \
    && useradd --system --uid 10001 --gid pms --home-dir /app --no-create-home --shell /usr/sbin/nologin pms

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config ./config
COPY data ./data
COPY docker/entrypoint.sh ./docker/entrypoint.sh

RUN mkdir -p /app/var && chown -R pms:pms /app/var

USER 10001:10001

EXPOSE 8000

# Container Apps uses the HTTP probes from Bicep; this HEALTHCHECK serves local docker runs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

CMD ["/bin/sh", "/app/docker/entrypoint.sh"]
