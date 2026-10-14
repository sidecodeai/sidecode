# remote-control gateway image.
#
# Build context is the repository ROOT, not gateway/: the image carries three artefacts that live in
# sibling directories — the built web app, the rc_client wheel the installer downloads, and the
# installer script itself. Build it through the root docker-compose.yml, or by hand with
#   docker build -f gateway/Dockerfile -t rc-gateway .
#
# `--target gateway` builds only the Python service, which is what the gateway tests need; the
# default target adds the web and client stages.

FROM node:22-alpine AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json* ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS client-build
WORKDIR /src/client
COPY client/ ./
RUN uv build --wheel --out-dir /out


FROM python:3.12-slim-bookworm AS gateway

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    RC_HOST=0.0.0.0 \
    RC_PORT=8787 \
    DATA_DIR=/data \
    WEB_DIST_DIR=/app/web/dist \
    CLIENT_DIST_DIR=/app/client/dist \
    CLIENT_INSTALL_SCRIPT=/app/client/install.sh

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so application edits do not invalidate the wheel cache.
COPY gateway/pyproject.toml gateway/uv.lock gateway/README.md ./gateway/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd gateway && uv sync --locked --no-install-project --no-dev

COPY gateway/rc_gateway/ ./gateway/rc_gateway/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd gateway && uv sync --locked --no-dev

# A dedicated unprivileged user owns the state volume, mirroring the systemd deployment.
RUN groupadd --gid 10001 rcgateway \
    && useradd --uid 10001 --gid rcgateway --home-dir /app --shell /usr/sbin/nologin rcgateway \
    && mkdir -p /data /app/web/dist /app/client/dist \
    && chown -R rcgateway:rcgateway /data /app

EXPOSE 8787
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('RC_PORT','8787')+'/api/health',timeout=4)" || exit 1

USER rcgateway
CMD ["rc-gateway"]


FROM gateway AS release

USER root
COPY --from=web-build --chown=rcgateway:rcgateway /src/web/dist /app/web/dist
COPY --from=client-build --chown=rcgateway:rcgateway /out/ /app/client/dist/
COPY --chown=rcgateway:rcgateway client/install.sh /app/client/install.sh
RUN chmod 0644 /app/client/install.sh
USER rcgateway
CMD ["rc-gateway"]
