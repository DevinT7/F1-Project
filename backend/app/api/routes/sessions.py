from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from ...auth.deps import admin_limit
from ...auth.store import User
from ...jobs.runner import JobSystem
from ..deps import get_job_system, get_session_service
from ..schemas.common import SESSION_KEY_DESCRIPTION, SESSION_KEY_PATTERN, canonical_session_key, error_responses
from ..schemas.jobs import JobResponse
from ..schemas.sessions import (
    CornersResponse,
    DriversResponse,
    FastestLapsResponse,
    LapsResponse,
    SessionStatusResponse,
    StintsResponse,
)
from ..services import SessionQueryService

router = APIRouter(prefix="/sessions", tags=["v1: sessions"])

SessionKey = Path(pattern=SESSION_KEY_PATTERN, description=SESSION_KEY_DESCRIPTION, examples=["2024-05-Q"])


@router.get("/{session_key}", response_model=SessionStatusResponse, summary="Session info and availability",
            description="`status` is `ready` (stored, current schema), `not_ingested`, or `stale_schema` "
                        "(stored by an older version: re-ingest). `info` is present only when ready.",
            responses=error_responses(422))
def get_session(session_key: str = SessionKey,
                service: SessionQueryService = Depends(get_session_service)) -> SessionStatusResponse:
    return service.status(session_key)


@router.post("/{session_key}/ingest", response_model=JobResponse, status_code=202, summary="Ingest a session (job)",
             description="Queues a background job that downloads (FastF1) and stores the session, and returns it "
                         "(202, `Location: /api/v1/jobs/{id}`). Poll `GET /api/v1/jobs/{id}` for progress: a cold "
                         "qualifying session takes ~2.5 min, a race ~7.5 min. Idempotent: an already-stored session "
                         "finishes at once with `result.skipped: true` unless `force=true`; a second request while "
                         "one is running returns the running job.",
             responses=error_responses(401, 403, 422, 429))
def ingest_session(
    response: Response,
    session_key: str = SessionKey,
    force: bool = Query(False, description="re-ingest even if already stored"),
    jobs: JobSystem = Depends(get_job_system),
    user: User = Depends(admin_limit),
) -> JobResponse:
    job = jobs.submit("ingest", canonical_session_key(session_key), {"force": force}, created_by=user.id)
    job = jobs.store.get(job.id) or job                # an inline queue may have finished it already
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return JobResponse.of(job)


@router.get("/{session_key}/drivers", response_model=DriversResponse, summary="Drivers in a session",
            description="Code, number, name, team, team colour (`#RRGGBB`) and classification.",
            responses=error_responses(404, 422))
def list_drivers(session_key: str = SessionKey,
                 service: SessionQueryService = Depends(get_session_service)) -> DriversResponse:
    return service.drivers(session_key)


@router.get("/{session_key}/laps", response_model=LapsResponse, summary="Laps",
            description="Every lap with times (s), compound, tyre age, weather at lap start and "
                        "`is_valid_for_comparison` / `invalid_reason`. Filter with `driver`.",
            responses=error_responses(404, 422))
def list_laps(
    session_key: str = SessionKey,
    driver: str | None = Query(None, description="driver code (VER) or number (1)", examples=["VER"]),
    service: SessionQueryService = Depends(get_session_service),
) -> LapsResponse:
    return service.laps(session_key, driver)


@router.get("/{session_key}/fastest-laps", response_model=FastestLapsResponse, summary="Fastest valid lap per driver",
            description="Lowest lap time among laps valid for comparison, per driver, fastest first. "
                        "Drivers with no valid lap are listed last with `lap: null`.",
            responses=error_responses(404, 422))
def fastest_laps(session_key: str = SessionKey,
                 service: SessionQueryService = Depends(get_session_service)) -> FastestLapsResponse:
    return service.fastest_laps(session_key)


@router.get("/{session_key}/stints", response_model=StintsResponse, summary="Tyre stints per driver",
            description="Per driver: stint number, compound, first/last lap, tyre age at start, new or used "
                        "set, total laps, clean laps (after the Phase 6A exclusion rules) and whether the stint "
                        "is model-eligible (≥ 5 clean laps).",
            responses=error_responses(404, 422))
def list_stints(session_key: str = SessionKey,
                service: SessionQueryService = Depends(get_session_service)) -> StintsResponse:
    return service.stints(session_key)


@router.get("/{session_key}/corners", response_model=CornersResponse, summary="Circuit corner table",
            description="Official corners (number, letter, distance along the lap, X/Y in FastF1 decimetres, "
                        "angle) and the map rotation in degrees.",
            responses=error_responses(404, 422))
def list_corners(session_key: str = SessionKey,
                 service: SessionQueryService = Depends(get_session_service)) -> CornersResponse:
    return service.corners(session_key)
