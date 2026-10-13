"""Error envelope, exception → status mapping, request ids, size and rate limits.

Every error response body is {"error": {"code", "message", "status_code", "request_id"}}
(plus "details" for validation errors). Stack traces never reach the client; the 500
handler logs the exception with the request id so it can be found server-side.
"""
from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..ingestion.errors import (
    DriverNotFoundError,
    IngestionError,
    LapInvalidError,
    LapNotFoundError,
    PayloadTooLargeError,
    SessionNotFoundError,
    TelemetryUnavailableError,
)
from ..processing.errors import InsufficientDataError
from ..tires.registry import ChecksumMismatchError, ModelNotAvailableError
from ..tires.service import NotARaceSessionError

logger = logging.getLogger("raceiq.api")

REQUEST_ID_HEADER = "X-Request-ID"
MAX_BODY_BYTES = 64 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Most specific first; lookup walks this list with isinstance.
ERROR_MAP: list[tuple[type[Exception], int, str]] = [
    (DriverNotFoundError, 404, "driver_not_found"),
    (LapNotFoundError, 404, "lap_not_found"),
    (SessionNotFoundError, 404, "session_not_found"),
    (InsufficientDataError, 422, "insufficient_data"),
    (NotARaceSessionError, 422, "not_a_race_session"),
    (ChecksumMismatchError, 503, "model_checksum_mismatch"),
    (ModelNotAvailableError, 503, "model_not_available"),
    (LapInvalidError, 422, "lap_invalid"),
    (TelemetryUnavailableError, 424, "telemetry_unavailable"),
    (PayloadTooLargeError, 413, "payload_too_large"),
    (IngestionError, 422, "ingestion_error"),
]


def request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def error_body(*, code: str, message: str, status_code: int, request: Request | None = None,
               details: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "status_code": status_code,
        "request_id": request_id(request) if request is not None else None,
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


def error_response(request: Request, *, code: str, message: str, status_code: int,
                   details: Any | None = None) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=error_body(code=code, message=message, status_code=status_code, request=request, details=details),
    )
    rid = request_id(request)
    if rid:
        response.headers[REQUEST_ID_HEADER] = rid
    return response


def classify(exc: Exception) -> tuple[int, str]:
    for cls, status, code in ERROR_MAP:
        if isinstance(exc, cls):
            return status, code
    return 500, "internal_error"


class RequestContextMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware: that re-streams large bodies through an extra task).

    Assigns a request id (honours a safe incoming X-Request-ID), stores it in request.state,
    echoes it as a response header, and rejects bodies over MAX_BODY_BYTES with 413.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        incoming = headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        length = headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
            response = error_response(Request(scope), code="payload_too_large", status_code=413,
                                      message=f"request body exceeds {MAX_BODY_BYTES} bytes")
            await response(scope, receive, send)
            return

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        await self.app(scope, receive, send_with_id)


RateLimiter = Callable[[Request], bool]


def allow_all(request: Request) -> bool:
    return True


class RateLimitMiddleware:
    """Placeholder: plug a real limiter in Phase 8 (e.g. Redis token bucket per client)."""

    def __init__(self, app: ASGIApp, limiter: RateLimiter = allow_all, prefix: str = "/api/v1") -> None:
        self.app = app
        self.limiter = limiter
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            request = Request(scope)
            if not self.limiter(request):
                response = error_response(request, code="rate_limited", status_code=429, message="too many requests")
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    status, code = classify(exc)
    return error_response(request, code=code, message=str(exc), status_code=status)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error (request_id=%s) on %s %s", request_id(request), request.method, request.url.path)
    return error_response(request, code="internal_error", status_code=500,
                          message="unexpected server error; quote the request_id when reporting it")


def install(app: FastAPI, *, limiter: RateLimiter = allow_all) -> None:
    for cls, _, _ in ERROR_MAP:
        app.add_exception_handler(cls, _domain_error)
    app.add_exception_handler(Exception, _unexpected_error)
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    app.add_middleware(RequestContextMiddleware)  # added last = outermost: every response gets an id
