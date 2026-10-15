"""Phase 8 job lifecycle: queued → running → succeeded | retrying → … | failed.

    submit(kind, target, params)  creates the record (or returns the active one for the same
                                  target: two "ingest 2024-05-Q" clicks run once) and enqueues it
    run_job(job_id)               what the worker executes: one attempt, with progress updates

Retries: an unexpected error (network, FastF1 hiccup) is retried up to max_attempts with
exponential backoff (5 s, 20 s, …); a PermanentJobError or a domain error (session does not
exist, invalid key) fails at once. Error text stored for the client is the exception type and
message only (no traceback, no secrets).

Queues (app/jobs/queues.py): RQ on Redis in production (the worker container), a thread pool
for local dev without Redis, and an inline queue for tests.
"""
from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import structlog

from ..observability import job_metrics, sentry_message
from .store import Job, JobKind, JobStore

log = structlog.get_logger("raceiq.jobs")

BACKOFF_S = (5.0, 20.0, 60.0)
Progress = Callable[[float, str], None]
Handler = Callable[[Job, Progress], dict[str, Any]]


class PermanentJobError(Exception):
    """Retrying cannot help (bad input, data that does not exist)."""


class Queue(Protocol):
    def enqueue(self, job_id: str, delay_s: float = 0.0) -> None: ...


@dataclass
class JobSystem:
    store: JobStore
    queue: Queue
    handlers: dict[str, Handler] = field(default_factory=dict)
    permanent: tuple[type[BaseException], ...] = (PermanentJobError,)
    sleep: Callable[[float], None] = time.sleep

    def submit(self, kind: JobKind, target: str, params: dict[str, Any] | None = None, *,
               max_attempts: int = 3, created_by: Any = None) -> Job:
        from .store import new_job
        active = self.store.active_for(kind, target)
        if active is not None:
            return active
        job = self.store.create(new_job(kind, target, params, max_attempts, created_by))
        log.info("job.queued", job_id=job.id, kind=kind, target=target)
        self.queue.enqueue(job.id)
        return job

    def run_job(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        if job is None:
            raise LookupError(f"unknown job {job_id}")
        if job.status in ("succeeded", "failed"):
            return job
        handler = self.handlers.get(job.kind)
        if handler is None:
            return self.store.update(job_id, status="failed", error=f"no handler for {job.kind!r} jobs")
        attempt = job.attempts + 1
        job = self.store.update(job_id, status="running", attempts=attempt, run_after=None, error=None,
                                message="started")
        log.info("job.running", job_id=job_id, kind=job.kind, target=job.target, attempt=attempt)
        last = [0.0]

        def progress(fraction: float, message: str) -> None:
            fraction = max(last[0], min(1.0, float(fraction)))   # never goes backwards
            last[0] = fraction
            self.store.update(job_id, progress=round(fraction, 3), message=message[:200])

        started = time.monotonic()
        try:
            result = handler(job, progress)
        except BaseException as exc:                              # noqa: BLE001 — the job records every failure
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            log.warning("job.attempt_failed", job_id=job_id, attempt=attempt, error=type(exc).__name__,
                        trace=traceback.format_exc(limit=5))
            if isinstance(exc, self.permanent) or attempt >= job.max_attempts:
                log.error("job.failed", job_id=job_id, kind=job.kind, attempts=attempt, error=type(exc).__name__)
                _observe(job.kind, "failed", time.monotonic() - started)
                sentry_message(f"{job.kind} job failed", level="error", fingerprint=["job-failed", job.kind],
                               tags={"job_kind": job.kind, "target": job.target, "error": type(exc).__name__})
                return self.store.update(job_id, status="failed", error=error, message="failed")
            delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
            job = self.store.update(job_id, status="retrying", error=error, message=f"retrying in {delay:.0f} s",
                                    run_after=datetime.now(timezone.utc) + timedelta(seconds=delay))
            self.queue.enqueue(job_id, delay_s=delay)
            _observe(job.kind, "retrying", time.monotonic() - started)
            return job
        _observe(job.kind, "succeeded", time.monotonic() - started)
        log.info("job.succeeded", job_id=job_id, elapsed_s=round(time.monotonic() - started, 1))
        return self.store.update(job_id, status="succeeded", progress=1.0, message="done", result=result)


def _observe(kind: str, outcome: str, seconds: float) -> None:
    """Job duration / outcome metrics (shared through Redis; app/observability.py). Never fails a job."""
    try:
        job_metrics().record_job(kind, outcome, seconds)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("job.metrics_unavailable", error=type(exc).__name__)
