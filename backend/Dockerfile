# Build context must be the repository root (see infra/docker-compose.yml):
#   docker build -f backend/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY core/ ./core/
COPY backend/ ./backend/

RUN python -m pip install --upgrade pip \
    && python -m pip install -e ./core \
    && python -m pip install -e ./backend

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
