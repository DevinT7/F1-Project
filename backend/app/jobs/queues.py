"""Job queues: where run_job executes.

  RQQueue      Redis + RQ; the `worker` container runs `python -m app.jobs.worker`
  ThreadQueue  a small in-process pool (local dev without Redis; jobs die with the process)
  InlineQueue  runs the job immediately in the caller (tests); delayed retries run right away
               unless `defer_retries` is set
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

QUEUE_NAME = "raceiq"
JOB_TIMEOUT_S = 3 * 3600


def _run(job_id: str) -> None:
    """The RQ task: run one attempt with the process's job system."""
    from .system import get_job_system
    get_job_system().run_job(job_id)


class InlineQueue:
    def __init__(self, run: Callable[[str], Any] | None = None, defer_retries: bool = False) -> None:
        self.run = run
        self.defer_retries = defer_retries
        self.deferred: list[tuple[str, float]] = []

    def enqueue(self, job_id: str, delay_s: float = 0.0) -> None:
        if delay_s > 0 and self.defer_retries:
            self.deferred.append((job_id, delay_s))
            return
        assert self.run is not None
        self.run(job_id)


class ThreadQueue:
    def __init__(self, run: Callable[[str], Any], workers: int = 2) -> None:
        self.run = run
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="raceiq-job")

    def enqueue(self, job_id: str, delay_s: float = 0.0) -> None:
        if delay_s > 0:
            timer = threading.Timer(delay_s, lambda: self.pool.submit(self.run, job_id))
            timer.daemon = True
            timer.start()
        else:
            self.pool.submit(self.run, job_id)


class RQQueue:
    def __init__(self, redis_url: str) -> None:
        import redis
        from rq import Queue
        self.connection = redis.Redis.from_url(redis_url)
        self.queue = Queue(QUEUE_NAME, connection=self.connection, default_timeout=JOB_TIMEOUT_S)

    def enqueue(self, job_id: str, delay_s: float = 0.0) -> None:
        from datetime import timedelta
        if delay_s > 0:
            self.queue.enqueue_in(timedelta(seconds=delay_s), _run, job_id)
        else:
            self.queue.enqueue(_run, job_id, job_id=job_id)
