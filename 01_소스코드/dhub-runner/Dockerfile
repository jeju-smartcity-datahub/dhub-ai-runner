FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY backend ./backend
RUN pip install --no-cache-dir ".[k8s]"
COPY frontend ./frontend

ENV RUNNER_COLLECTION_DIR=/collection \
    RUNNER_DATA_DIR=/data \
    RUNNER_FRONTEND_DIR=/app/frontend \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["dhub-runner"]
