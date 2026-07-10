"""Shared helpers for OpenF1-backed routers."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from ..clients.openf1 import (
    OpenF1Error,
    OpenF1Result,
    ProviderBadResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def envelope(result: OpenF1Result) -> dict[str, Any]:
    return {"data": result.data, "meta": result.meta}


def provider_http_exception(exc: OpenF1Error) -> HTTPException:
    if isinstance(exc, ProviderRateLimitError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (ProviderTimeoutError, ProviderUnavailableError)):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ProviderBadResponseError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))
