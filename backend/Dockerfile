# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f backend/Dockerfile .
FROM python:3.12.11-slim

# Run as a non-root user; only the runtime tree below is writable.
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY core/ ./core/
COPY backend/ ./backend/

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ./core ./backend

# Resolve `app` and `smart_commissioning_core` from the source trees instead
# of site-packages: the runtime root must stay at /app/backend/runtime (where
# compose mounts the api_runtime volume), and core's migrate.py locates
# alembic.ini/alembic/ relative to its own __file__ (they live next to the
# package in /app/core, not inside site-packages).
ENV PYTHONPATH=/app/backend:/app/core

# The API writes file artifacts under backend/runtime: the SQLite database
# (when no DATABASE_URL is set), uploaded import files, and secret material.
# Pre-create it owned by the app user; a named volume mounted here inherits
# this ownership on first use.
RUN mkdir -p /app/backend/runtime && chown -R app:app /app/backend/runtime

USER app

WORKDIR /app/backend

EXPOSE 8000

# python:slim ships no curl/wget; probe the readiness endpoint with stdlib
# urllib. urlopen raises (non-zero exit) on connection errors and on 503.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=5)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
