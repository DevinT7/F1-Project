"""Job records: what GET /jobs/{id} returns. Postgres (ingest_jobs) when a database is
configured, else in memory (single-process dev). The queue (RQ or a thread) only executes;
status, progress, attempts and errors live here."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from sqlalchemy import Engine, insert, select, update

from ..db.schema import ingest_jobs

JobKind = Literal["ingest", "train"]
JobStatus = Literal["queued", "running", "retrying", "succeeded", "failed"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed"})


@dataclass(frozen=True)
class Job:
    id: str
    kind: JobKind
    target: str
    status: JobStatus = "queued"
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    run_after: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    created_by: uuid.UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def new_job(kind: JobKind, target: str, params: dict[str, Any] | None = None, max_attempts: int = 3,
            created_by: uuid.UUID | None = None) -> Job:
    return Job(id=uuid.uuid4().hex, kind=kind, target=target, params=params or {}, max_attempts=max_attempts,
               created_by=created_by)


class JobStore(Protocol):
    def create(self, job: Job) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def update(self, job_id: str, **changes: Any) -> Job: ...
    def active_for(self, kind: JobKind, target: str) -> Job | None: ...


class MemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: Any) -> Job:
        with self._lock:
            job = replace(self._jobs[job_id], **changes, updated_at=datetime.now(timezone.utc))
            self._jobs[job_id] = job
            return job

    def active_for(self, kind: JobKind, target: str) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values()
                         if j.kind == kind and j.target == target and j.status not in TERMINAL), None)


_COLUMNS = ("id", "kind", "target", "status", "progress", "message", "error", "attempts", "max_attempts", "run_after",
            "params", "result", "created_by", "created_at", "updated_at")


class PostgresJobStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _job(row: Any) -> Job:
        return Job(**{c: row[c] for c in _COLUMNS})

    def create(self, job: Job) -> Job:
        with self.engine.begin() as conn:
            conn.execute(insert(ingest_jobs).values(**{c: getattr(job, c) for c in _COLUMNS}))
        return job

    def get(self, job_id: str) -> Job | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(ingest_jobs).where(ingest_jobs.c.id == job_id)).mappings().first()
        return None if row is None else self._job(row)

    def update(self, job_id: str, **changes: Any) -> Job:
        with self.engine.begin() as conn:
            row = conn.execute(update(ingest_jobs).where(ingest_jobs.c.id == job_id)
                               .values(**changes, updated_at=datetime.now(timezone.utc))
                               .returning(*ingest_jobs.c)).mappings().one()
        return self._job(row)

    def active_for(self, kind: JobKind, target: str) -> Job | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(ingest_jobs).where(
                ingest_jobs.c.kind == kind, ingest_jobs.c.target == target,
                ingest_jobs.c.status.not_in(sorted(TERMINAL))).order_by(ingest_jobs.c.created_at.desc())
            ).mappings().first()
        return None if row is None else self._job(row)
