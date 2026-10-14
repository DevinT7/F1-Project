"""RaceIQ FastAPI entrypoint."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from . import cache
from .api import errors as api_errors
from .api.routes import TAGS_METADATA as V1_TAGS
from .api.routes import router as v1_router
from .clients import fastf1_client
from .clients.openf1 import (
    OpenF1Error,
    ProviderBadResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from .config import allowed_origins, settings
from .routers import (
    ai,
    analysis,
    drivers,
    laps,
    sessions,
    stints,
    telemetry,
    tires,
    weather,
)

API_PREFIX = "/api"


def _app_version() -> str:
    return os.getenv("APP_VERSION") or __version__


def _split_origins(value: str | None) -> list[str]:
    return [origin.strip() for origin in (value or "").split(",") if origin.strip()]


def _cors_origins() -> list[str]:
    return allowed_origins()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    details: Any | None = None,
) -> JSONResponse:
    # One envelope for the whole API: {"error": {code, message, status_code, request_id[, details]}}
    return api_errors.error_response(
        request, code=code, message=message, status_code=status_code, details=details
    )


app = FastAPI(
    title="RaceIQ API",
    version=_app_version(),
    description="AI-powered Formula 1 telemetry analytics API.",
    docs_url="/docs",
    openapi_tags=V1_TAGS,
)

api_errors.install(app, limiter=api_errors.default_limiter)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    status_code = exc.status_code
    message = str(exc.detail or "Request failed")
    code = "http_error"
    if status_code == 404:
        code = "not_found"
    elif status_code == 502:
        code = "provider_error"
    elif status_code == 400:
        code = "bad_request"
    return _error_response(request, code=code, message=message, status_code=status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _error_response(
        request,
        code="validation_error",
        message="Request validation failed",
        status_code=422,
        details=jsonable_encoder(exc.errors()),
    )


@app.exception_handler(OpenF1Error)
async def provider_exception_handler(request: Request, exc: OpenF1Error) -> JSONResponse:
    status_code = 502
    if isinstance(exc, (ProviderRateLimitError, ProviderTimeoutError, ProviderUnavailableError)):
        status_code = 503
    elif isinstance(exc, ProviderBadResponseError):
        status_code = 502
    return _error_response(request, code="provider_error", message=str(exc), status_code=status_code)


@app.get(f"{API_PREFIX}/health", tags=["meta"])
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "cache": cache.status(),
        "fastf1": fastf1_client.status(),
        "timestamp": _utc_timestamp(),
        "version": _app_version(),
    }


@app.get("/health", include_in_schema=False)
async def legacy_health() -> dict[str, Any]:
    return await health()


@app.get(f"{API_PREFIX}/data-status", tags=["meta"])
async def data_status() -> dict[str, Any]:
    return {
        "cache": {
            "status": cache.status(),
            "backend": cache.backend_name(),
        },
        "fastf1": fastf1_client.status(),
        "openf1": {
            "status": "configured",
            "base_url": settings.openf1_base_url,
        },
    }


app.include_router(sessions.router, prefix=API_PREFIX)
app.include_router(drivers.router, prefix=API_PREFIX)
app.include_router(laps.router, prefix=API_PREFIX)
app.include_router(telemetry.router, prefix=API_PREFIX)
app.include_router(stints.router, prefix=API_PREFIX)
app.include_router(weather.router, prefix=API_PREFIX)
app.include_router(analysis.router, prefix=API_PREFIX)
app.include_router(tires.router, prefix=API_PREFIX)
app.include_router(ai.router, prefix=API_PREFIX)
app.include_router(v1_router)
