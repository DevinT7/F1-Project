"""Phase 8 background job responses (POST .../ingest → 202, GET /jobs/{id})."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...jobs.store import Job


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["ingest", "train"]
    target: str = Field(description="session key (ingest) or model name (train)")
    status: Literal["queued", "running", "retrying", "succeeded", "failed"]
    progress: float = Field(ge=0, le=1, description="0–1; never goes backwards")
    message: str | None
    error: str | None = Field(description="exception type and message of the last failed attempt")
    attempts: int
    max_attempts: int
    run_after: datetime | None = Field(description="when a retry is scheduled")
    result: dict[str, Any] | None = Field(description="ingest: session_key, skipped, laps, telemetry_laps, valid_laps, "
                                                      "elapsed_s")
    created_at: datetime
    updated_at: datetime
    status_url: str

    @classmethod
    def of(cls, job: Job) -> JobResponse:
        target = job.target.replace("_", "-") if job.kind == "ingest" else job.target
        return cls(id=job.id, kind=job.kind, target=target, status=job.status, progress=job.progress,
                   message=job.message, error=job.error, attempts=job.attempts, max_attempts=job.max_attempts,
                   run_after=job.run_after, result=job.result, created_at=job.created_at, updated_at=job.updated_at,
                   status_url=f"/api/v1/jobs/{job.id}")
