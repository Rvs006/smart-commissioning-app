# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f worker/Dockerfile .
FROM python:3.12.11-slim

# Run as a non-root user; the worker writes no local files (state lives in
# Postgres and Redis).
RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app

COPY core/ ./core/
COPY worker/ ./worker/

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ./core ./worker

# Resolve `app` and `smart_commissioning_core` from the source trees instead
# of site-packages so path-derived defaults (the dev SQLite fallback, core's
# alembic script location) stay anchored to /app.
ENV PYTHONPATH=/app/worker:/app/core

USER app

WORKDIR /app/worker

CMD ["dramatiq", "app.tasks"]
