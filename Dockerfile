# syntax=docker/dockerfile:1
# Pool & Spa guest web UI. Listens on $PORT (8080), runs as uid 1000.
ARG PYTHON_VERSION=3.14

FROM ghcr.io/astral-sh/uv:0.12 AS uv

FROM python:${PYTHON_VERSION}-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
# git: iaqualink-py is pinned to a git commit (see pyproject.toml).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PATH=/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
COPY --from=build /venv /venv
WORKDIR /srv
COPY app ./app
# Saved owner settings (CONFIG_PATH=/data/config.json). Mount a volume here; the
# directory is owned by the app user so a fresh Docker named volume is writable.
RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8080
# --proxy-headers applies X-Forwarded-For/-Proto only from the addresses in
# $FORWARDED_ALLOW_IPS (uvicorn's default: 127.0.0.1), never from any peer: a LAN
# client must not choose its own address (the owner PIN rate limit keys on it).
# The app reads X-Forwarded-Proto itself for the Secure cookie, and
# OWNER_TRUSTED_PROXIES decides whose X-Forwarded-For the PIN limit believes.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
