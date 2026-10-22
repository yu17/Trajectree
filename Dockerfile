FROM node:22-alpine AS web
RUN corepack enable
WORKDIR /build/web
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build

FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY --from=web /build/src/trajectree/static ./src/trajectree/static
RUN uv sync --frozen --no-dev
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin trajectree \
    && mkdir /data && chown 1000:1000 /data
ENV TRAJECTREE_DATA_DIR=/data \
    TRAJECTREE_HOST=0.0.0.0 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8742
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8742/api/health').status==200 else 1)"
USER trajectree
ENTRYPOINT ["/app/docker/entrypoint.sh"]
