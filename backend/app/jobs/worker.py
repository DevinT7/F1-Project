"""The background worker (docker-compose `worker` service):

    cd backend && python -m app.jobs.worker

Runs RQ on the `raceiq` queue with the scheduler (delayed retries). Needs RACEIQ_REDIS_URL, and
RACEIQ_DATABASE_URL so job status is shared with the API. Linux / macOS (RQ forks per job).
"""
from __future__ import annotations

import sys

import structlog

from ..config import settings
from .queues import QUEUE_NAME

log = structlog.get_logger("raceiq.jobs")


def main() -> int:
    if not settings.redis_url:
        print("RACEIQ_REDIS_URL is required for the worker", file=sys.stderr)
        return 2
    import redis
    from rq import Worker
    connection = redis.Redis.from_url(settings.redis_url)
    log.info("worker.start", queue=QUEUE_NAME, database=bool(settings.database_url))
    Worker([QUEUE_NAME], connection=connection).work(with_scheduler=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
