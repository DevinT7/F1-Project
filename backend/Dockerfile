# RaceIQ backend: API and worker images from one Dockerfile (Phase 9).
#
#   docker build --target api    -t raceiq-backend ./backend     # FastAPI (uvicorn), healthcheck
#   docker build --target worker -t raceiq-worker  ./backend     # RQ worker (ingest / training jobs)
#
# Multi-stage: dependencies are installed into a virtualenv in a builder stage; the runtime stages
# copy only that venv and the app, run as a non-root user, and never contain build tools or caches.
# Base images are pinned by digest (Dependabot keeps them current: .github/dependabot.yml).

ARG PYTHON_IMAGE=python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89

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
WORKDIR /app
COPY --chown=raceiq . .
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
# limits are shared through Redis; size it to the instance's cores.
ENV WEB_CONCURRENCY=4
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips '*'"]
