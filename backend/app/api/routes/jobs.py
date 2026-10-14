from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse

from ...jobs.runner import JobSystem
from ..deps import get_job_system
from ..errors import error_response
from ..schemas.common import error_responses
from ..schemas.jobs import JobResponse

router = APIRouter(prefix="/jobs", tags=["v1: jobs"])


@router.get("/{job_id}", response_model=JobResponse, summary="Background job status",
            description="Status and progress of an ingest or training job (poll every 1–2 s until `succeeded` "
                        "or `failed`). Failed attempts are retried with backoff up to `max_attempts` unless the "
                        "error is permanent (e.g. the session does not exist).",
            responses=error_responses(404))
def get_job(request: Request, job_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
            jobs: JobSystem = Depends(get_job_system)) -> JobResponse | JSONResponse:
    job = jobs.store.get(job_id)
    if job is None:
        return error_response(request, code="job_not_found", status_code=404, message=f"no job {job_id}")
    return JobResponse.of(job)
