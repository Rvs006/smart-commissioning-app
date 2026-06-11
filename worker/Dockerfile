# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f worker/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY core/ ./core/
COPY worker/ ./worker/

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ./core \
    && python -m pip install -e ./worker

WORKDIR /app/worker

CMD ["dramatiq", "app.tasks"]
