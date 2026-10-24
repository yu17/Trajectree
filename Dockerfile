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
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client ripgrep && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/claude && HOME=/opt/claude bash -c "curl -fsSL https://claude.ai/install.sh | bash" \
    && chmod -R a+rX /opt/claude && /opt/claude/.local/bin/claude --version
# Optional Codex runner: --build-arg INSTALL_CODEX=1. The release publishes no .sha256 file, so
# CODEX_SHA256 is the GitHub asset digest of codex-x86_64-unknown-linux-musl.tar.gz for
# CODEX_VERSION; change both together.
ARG INSTALL_CODEX=0
ARG CODEX_VERSION=0.163.0
ARG CODEX_SHA256=beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4
RUN if [ "$INSTALL_CODEX" = "1" ]; then \
        mkdir -p /opt/codex/bin /tmp/codex \
        && curl -fsSL -o /tmp/codex/codex.tar.gz \
            "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-x86_64-unknown-linux-musl.tar.gz" \
        && echo "${CODEX_SHA256}  /tmp/codex/codex.tar.gz" | sha256sum -c - \
        && tar -xzf /tmp/codex/codex.tar.gz -C /tmp/codex codex-x86_64-unknown-linux-musl \
        && mv /tmp/codex/codex-x86_64-unknown-linux-musl /opt/codex/bin/codex \
        && chmod a+rx /opt/codex/bin/codex \
        && rm -rf /tmp/codex \
        && /opt/codex/bin/codex --version; \
    fi
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin trajectree \
    && mkdir /data && chown 1000:1000 /data \
    && mkdir /cfg && chown 1000:1000 /cfg
# TRAJECTREE_CODEX_BIN is set even without Codex: it is only used when a run picks codex.
ENV TRAJECTREE_DATA_DIR=/data \
    TRAJECTREE_CFG_DIR=/cfg \
    HOME=/cfg/home \
    DISABLE_AUTOUPDATER=1 \
    TRAJECTREE_HOST=0.0.0.0 \
    TRAJECTREE_CODEX_BIN=/opt/codex/bin/codex \
    PATH=/opt/claude/.local/bin:/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1
VOLUME /data
EXPOSE 8742
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8742/api/health').status==200 else 1)"
USER trajectree
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# PDF and PPTX report exports: marp-cli with Debian's Chromium.
# TRAJECTREE_IMAGE_TARGET=reports docker compose up -d --build
FROM app AS reports
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm chromium fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && HOME=/root npm install -g @marp-team/marp-cli@4.5.1 \
    && HOME=/root npm cache clean --force
ENV CHROME_NO_SANDBOX=1 \
    TRAJECTREE_MARP_BIN=/usr/local/bin/marp \
    TRAJECTREE_CHROME_BIN=/usr/bin/chromium
RUN mkdir -p /tmp/marp-check \
    && HOME=/tmp/marp-check marp --version \
    && printf '# hi\n' > /tmp/t.md \
    && HOME=/tmp/marp-check marp /tmp/t.md -o /tmp/t.pdf --browser-path /usr/bin/chromium \
    && test -s /tmp/t.pdf \
    && rm -rf /tmp/t.md /tmp/t.pdf /tmp/marp-check
USER trajectree
