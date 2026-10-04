# Bianque API image: the FastAPI app plus the serving slice built by gold.
# Only the API's dependencies are installed (no dev or ml groups). The slice is copied from
# the local build context (it is git-ignored, never committed); make deploy checks it exists.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.6.10 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

# Dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

COPY configs ./configs
COPY policies ./policies
COPY models ./models
COPY data/gold/serving/serving.duckdb ./data/gold/serving/serving.duckdb

RUN useradd --create-home --uid 1000 bianque
USER bianque

CMD ["sh", "-c", "uvicorn bianque.api.main:app --host 0.0.0.0 --port ${PORT}"]
