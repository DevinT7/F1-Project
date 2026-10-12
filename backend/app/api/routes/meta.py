from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ... import __version__
from ...storage.repository import SessionRepository
from ..deps import get_repository
from ..schemas.common import error_responses
from ..schemas.sessions import HealthResponse, ReadyResponse

router = APIRouter(tags=["v1: meta"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe",
            description="Always 200 while the process is serving requests. Does not touch storage.")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=os.getenv("APP_VERSION") or __version__)


@router.get("/ready", response_model=ReadyResponse, summary="Readiness probe",
            description="200 when the session store is readable; 503 otherwise.",
            responses=error_responses(503))
def ready(repo: SessionRepository = Depends(get_repository)) -> ReadyResponse | JSONResponse:
    checks: dict[str, bool] = {}
    try:
        repo.list_sessions()
        checks["session_store"] = True
    except Exception:
        checks["session_store"] = False
    body = ReadyResponse(status="ready" if all(checks.values()) else "not_ready", checks=checks)
    if body.status != "ready":
        return JSONResponse(status_code=503, content=body.model_dump())
    return body
