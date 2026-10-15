"""The process's job system: store + queue + handlers, chosen from settings.

  store   PostgresJobStore when RACEIQ_DATABASE_URL is set, else MemoryJobStore
  queue   RQQueue when RACEIQ_REDIS_URL is set and RACEIQ_JOB_QUEUE != "thread", else ThreadQueue
          (the API process runs jobs itself: fine for local dev, lost on restart)
"""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

import structlog

from ..caching import get_cache, invalidate_session
from ..config import settings
from ..ingestion.errors import SessionNotFoundError
from ..ingestion.service import SessionSource
from ..storage.repository import SessionRepository
from .queues import RQQueue, ThreadQueue
from .runner import Handler, JobSystem, PermanentJobError, Progress
from .store import Job, JobStore, MemoryJobStore, PostgresJobStore

log = structlog.get_logger("raceiq.jobs")


def make_ingest_handler(repo_factory: Callable[[], SessionRepository],
                        source_factory: Callable[[], SessionSource]) -> Handler:
    """The ingest job: FastF1 → storage, then drop the session's cached comparisons."""
    def ingest_handler(job: Job, progress: Progress) -> dict[str, Any]:
        from ..ingestion.service import IngestionService
        year, rnd, code = job.target.split("_")
        repo = repo_factory()
        service = IngestionService(repo, source_factory())
        try:
            result = service.ingest(int(year), int(rnd), code, force=bool(job.params.get("force")), progress=progress)
        except (SessionNotFoundError, ValueError) as exc:
            raise PermanentJobError(str(exc)) from exc
        out: dict[str, Any] = {"session_key": result.key.replace("_", "-"), "skipped": result.skipped,
                               "laps": result.laps, "telemetry_laps": result.telemetry_laps,
                               "valid_laps": result.valid_laps, "elapsed_s": round(result.elapsed_s, 1)}
        if not result.skipped:
            invalidate_session(get_cache(), result.key)
            out.update(_score_drift(repo, result.key))
        return out

    def _score_drift(repo: SessionRepository, key: str) -> dict[str, Any]:
        # model drift on the newly ingested race (app/tires/drift.py); monitoring must never fail an ingest
        try:
            from ..api.deps import get_model_registry
            from ..observability import job_metrics
            from ..tires.drift import record_session
            return record_session(repo, key, get_model_registry(), job_metrics())
        except Exception as exc:                                   # noqa: BLE001
            log.warning("drift.unavailable", session=key, error=type(exc).__name__)
            return {"drift": "unavailable"}
    return ingest_handler


def _default_repo() -> SessionRepository:
    from ..api.deps import get_repository
    return get_repository()


def _default_source() -> SessionSource:
    from ..ingestion.fastf1_client import FastF1Client
    return FastF1Client(settings.fastf1_cache_dir)


def train_handler(job: Job, progress: Progress) -> dict[str, Any]:
    from ..tires.training_job import train_and_register
    return train_and_register(job.params, progress)


HANDLERS: dict[str, Handler] = {"ingest": make_ingest_handler(_default_repo, _default_source),
                                "train": train_handler}


@lru_cache(maxsize=1)
def get_job_system() -> JobSystem:
    store: JobStore
    if settings.database_url:
        from ..db.engine import make_engine
        store = PostgresJobStore(make_engine(settings.database_url))
    else:
        store = MemoryJobStore()
    system = JobSystem(store=store, queue=None, handlers=dict(HANDLERS),  # type: ignore[arg-type]
                       permanent=(PermanentJobError,))
    if settings.redis_url and settings.job_queue != "thread":
        system.queue = RQQueue(settings.redis_url)
    else:
        system.queue = ThreadQueue(system.run_job)
    return system
