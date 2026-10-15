# RaceIQ backend: API and worker images from one Dockerfile (Phase 9).
#
#   docker build --build-context models=./models --target api    -t raceiq-backend ./backend
#   docker build --build-context models=./models --target worker -t raceiq-worker  ./backend
#
# The tyre model registry (repo-root models/, outside this build context) is baked in at /models through
# the named build context `models` (docker compose and the deploy workflow pass it). Without it the image
# still builds, with an empty /models: the API then serves 503 for tyre forecasts until a registry exists.
#
# Multi-stage: dependencies are installed into a virtualenv in a builder stage; the runtime stages
# copy only that venv and the app, run as a non-root user, and never contain build tools or caches.
# Base images are pinned by digest (Dependabot keeps them current: .github/dependabot.yml).

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

# ---------------------------------------------------------------- models (overridden by --build-context models=…)
FROM scratch AS models

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools && pip install -r /tmp/requirements.txt

# ---------------------------------------------------------------- runtime base
FROM ${PYTHON_IMAGE} AS runtime
# libgomp: the lightgbm / xgboost runtime. The base image's own pip / setuptools / wheel are build
# tooling the runtime never uses (and they vendor packages with known CVEs): removed, so the image
# scan stays clean.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip setuptools wheel \
    && useradd --create-home --uid 10001 raceiq \
    && mkdir -p /cache /data && chown raceiq /cache /data
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LOG_FORMAT=json
COPY --from=builder /opt/venv /opt/venv
# nothing is installed at runtime: the venv's pip (and the urllib3 / msgpack / pkg_resources it vendors) goes too
RUN /opt/venv/bin/python -m pip uninstall -y pip
COPY --from=models --chown=raceiq . /models
ENV RACEIQ_TIRE_REGISTRY_PATH=/models/registry.json
WORKDIR /app
COPY --chown=raceiq . .
# the release (git tag or sha): /api/v1/health reports it, Sentry tags events with it, deploys verify it
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
USER raceiq

# ---------------------------------------------------------------- worker
FROM runtime AS worker
CMD ["python", "-m", "app.jobs.worker"]

# ---------------------------------------------------------------- api (last stage = the default target)
FROM runtime AS api
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]
# WEB_CONCURRENCY uvicorn workers (default 4): safe because the cache, jobs, conversations and rate
# limits are shared through Redis; size it to the instance's cores. PROMETHEUS_MULTIPROC_DIR lets /metrics
# sum all workers (emptied on every start, as prometheus_client requires). Uvicorn's access log is off:
# app/api/errors.py writes one structured http.request line per request instead (no client IPs).
ENV WEB_CONCURRENCY=4 PROMETHEUS_MULTIPROC_DIR=/tmp/raceiq-metrics
CMD ["sh", "-c", "rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\" && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips '*' --no-access-log"]
