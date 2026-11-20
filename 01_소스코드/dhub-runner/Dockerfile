FROM python:3.12-slim

# uv 는 버전을 고정해 가져옵니다. 설치는 uv.lock 그대로 재현됩니다.
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY backend ./backend
# 파이프라인 options.packages 와 커넥터 packages 는 실행 중에 `python -m pip install` 로
# 설치되므로(backend/commons/scripts.py) venv 를 seed 해서 pip 를 남기고,
# --inexact 로 uv sync 가 그 pip 를 지우지 않게 합니다.
RUN uv venv --seed /app/.venv \
    && uv sync --frozen --no-dev --extra k8s --no-editable --inexact --no-cache
ENV PATH="/app/.venv/bin:${PATH}"
COPY frontend ./frontend

ENV RUNNER_COLLECTION_DIR=/collection \
    RUNNER_DATA_DIR=/data \
    RUNNER_FRONTEND_DIR=/app/frontend \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["dhub-runner"]
