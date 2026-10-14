"""docker-compose seed (Phase 8): bring a fresh stack to a usable state, idempotently.

    python -m scripts.seed

  1. wait for PostgreSQL, apply the Alembic migrations
  2. load sessions into Postgres: the bundled sample (2024 R5 qualifying VER/PER/ALO + the synthetic
     race 2024 R99) from tests/fixtures/ingestion/store, plus everything under RACEIQ_SEED_DATA_DIR
     when SEED_FULL_DATA=1 (e.g. the repo's data/ mounted read-only; ~10 min for 25 races)
  3. wait for object storage, publish the tyre models (models/registry.json) to it + model_registry
Re-running skips sessions already present and upserts the models.
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.config import settings

T = TypeVar("T")
SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ingestion" / "store"


def retry(what: str, fn: Callable[[], T], attempts: int = 60, delay_s: float = 2.0) -> T:
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:                       # the service is still starting
            if i == attempts - 1:
                raise
            print(f"waiting for {what} ({type(exc).__name__})", flush=True)
            time.sleep(delay_s)
    raise AssertionError("unreachable")


def main() -> int:
    from app.db.engine import make_engine, upgrade
    from app.storage.postgres import PostgresRepository
    from app.storage.repository import ParquetRepository
    from scripts.migrate_parquet_to_postgres import migrate

    if not settings.database_url:
        print("RACEIQ_DATABASE_URL is required", file=sys.stderr)
        return 2
    url = settings.database_url
    retry("PostgreSQL", lambda: upgrade(url))
    target = PostgresRepository(make_engine(url))
    done = migrate(ParquetRepository(SAMPLE), target, only_missing=True)
    full = os.environ.get("RACEIQ_SEED_DATA_DIR")
    if os.environ.get("SEED_FULL_DATA") == "1" and full and Path(full).is_dir():
        done += migrate(ParquetRepository(full), target, only_missing=True)
    print(f"sessions loaded this run: {len(done)}; stored: {len(target.list_sessions())}", flush=True)

    if settings.model_registry == "db":
        from app.objectstore import S3ObjectStore, get_object_store
        from scripts.publish_models import publish

        def store() -> object:
            s = get_object_store()
            if isinstance(s, S3ObjectStore):
                s.ensure_bucket()
            return s

        objects = retry("object storage", store)
        for e in publish(Path(settings.tire_registry_path), make_engine(url), objects):  # type: ignore[arg-type]
            print(f"model {e.status:10} {e.name} {e.version}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
