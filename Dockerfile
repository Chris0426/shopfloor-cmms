# App Machine 映像(multi-stage,ADR-012/013)。自管 PG 的映像見 infra/postgres/。
# 給 FastAPI domain service + MCP server + pipeline worker 用。

# ---- builder:裝依賴到 venv ----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 先裝依賴(利用 layer cache);只複製建置必要檔
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

# ---- runtime:精簡執行映像 ----
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 非 root 執行
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

USER appuser
EXPOSE 8080

# 預設起 API;MCP / CLI / migration 由 fly.toml 的 process / release_command 指定
CMD ["uvicorn", "cmms.api.app:app", "--host", "0.0.0.0", "--port", "8080"]
