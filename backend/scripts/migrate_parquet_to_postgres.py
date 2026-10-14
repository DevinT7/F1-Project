"""Phase 8 data migration: every stored Parquet session → PostgreSQL (and the schedule cache).

    cd backend && python -m scripts.migrate_parquet_to_postgres [--data-dir ../data] [--database-url URL]
                                                                [--only-missing] [--sessions 2024_05_Q ...]

Idempotent: a session already in Postgres is replaced in one transaction (or skipped with
--only-missing). Telemetry goes in with COPY. The database URL comes from --database-url or
RACEIQ_DATABASE_URL (never hard-coded); migrations are applied first.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app.config import settings
from app.db.engine import make_engine, upgrade
from app.storage.postgres import PostgresRepository
from app.storage.repository import ParquetRepository, load_bundle


def migrate(source: ParquetRepository, target: PostgresRepository, keys: list[str] | None = None,
            only_missing: bool = False, log: bool = True) -> list[str]:
    done: list[str] = []
    for info in source.list_sessions():
        if keys and info.key not in keys:
            continue
        if only_missing and target.has_session(info.key):
            continue
        start = time.perf_counter()
        bundle = load_bundle(source, info.key)
        target.save_session(bundle)
        samples = sum(len(f) for f in bundle.telemetry.values())
        if log:
            print(f"{info.key}: {len(bundle.laps)} laps, {len(bundle.telemetry)} telemetry laps, {samples:,} samples "
                  f"in {time.perf_counter() - start:.1f} s", flush=True)
        done.append(info.key)
    for year in sorted({s.year for s in source.list_sessions()}):
        cached = source.get_events(year)
        if cached is not None:
            target.save_events(year, cached[1])
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.migrate_parquet_to_postgres")
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--database-url", default=os.environ.get("RACEIQ_DATABASE_URL") or settings.database_url)
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--sessions", nargs="*")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("set RACEIQ_DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    upgrade(args.database_url)
    done = migrate(ParquetRepository(args.data_dir), PostgresRepository(make_engine(args.database_url)),
                   args.sessions, args.only_missing)
    print(f"migrated {len(done)} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
