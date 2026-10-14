"""Phase 8 repository backends for the shared contract suite (tests/test_repository_contract.py).

    parquet    always: ParquetRepository on a temp directory
    postgres   RACEIQ_TEST_DATABASE_URL when set (CI service container); otherwise a throwaway
               TimescaleDB container via testcontainers when Docker is running; otherwise the
               postgres cases are SKIPPED with the reason (never silently passed)

The database is migrated once per test session with Alembic (the real migrations) and every
table is truncated before each test.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TIMESCALE_IMAGE = "timescale/timescaledb:2.17.2-pg16"
_DB: dict[str, Any] = {}


def _docker_running() -> bool:
    try:
        import docker  # type: ignore[import-untyped]
        docker.from_env().ping()
        return True
    except Exception:
        return False


def database_url() -> str:
    """A migrated database for this test session (or pytest.skip with the reason)."""
    if "url" in _DB:
        return str(_DB["url"])
    if "skip" in _DB:
        pytest.skip(_DB["skip"])
    url = os.environ.get("RACEIQ_TEST_DATABASE_URL")
    if not url:
        if not _docker_running():
            _DB["skip"] = "Postgres backend skipped: set RACEIQ_TEST_DATABASE_URL or start Docker (testcontainers)"
            pytest.skip(_DB["skip"])
        from testcontainers.postgres import PostgresContainer
        container = PostgresContainer(TIMESCALE_IMAGE, username="raceiq", password="raceiq", dbname="raceiq_test",
                                      driver="psycopg")
        container.start()
        _DB["container"] = container
        url = container.get_connection_url()
    from app.db.engine import make_engine, upgrade
    upgrade(url)
    _DB["url"] = url
    _DB["engine"] = make_engine(url)
    return url


def engine() -> Any:
    database_url()
    return _DB["engine"]


def truncate_all() -> None:
    from sqlalchemy import text
    from app.db.schema import metadata
    names = ", ".join(t.name for t in metadata.sorted_tables)
    with engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


def stop() -> None:
    eng = _DB.pop("engine", None)
    if eng is not None:
        eng.dispose()
    container = _DB.pop("container", None)
    if container is not None:
        container.stop()
    _DB.clear()


def make_repo(kind: str, tmp_path: Path) -> Iterator[Any]:
    if kind == "parquet":
        from app.storage.repository import ParquetRepository
        yield ParquetRepository(tmp_path / "data")
        return
    from app.storage.postgres import PostgresRepository
    url = database_url()
    truncate_all()
    yield PostgresRepository(engine())
    del url
