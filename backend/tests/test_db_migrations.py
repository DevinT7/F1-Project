"""Phase 8 — migrations are reversible and match the schema; Parquet → Postgres data migration.

Needs a database (RACEIQ_TEST_DATABASE_URL or Docker for testcontainers); skipped otherwise,
like the Postgres half of the repository contract suite.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from app.db.engine import downgrade, include_object, upgrade
from app.db.schema import metadata
from app.ingestion import models
from app.storage.postgres import PostgresRepository
from app.storage.repository import ParquetRepository
from scripts.migrate_parquet_to_postgres import migrate
from tests import repository_backends as backends

FIXTURE_STORE = Path(__file__).parent / "fixtures" / "ingestion" / "store"


@pytest.fixture(scope="module", autouse=True)
def _stop_container():
    yield
    backends.stop()


def tables(url: str) -> set[str]:
    with backends.engine().connect() as conn:
        return set(inspect(conn).get_table_names()) - {"alembic_version"}


def test_migrations_are_reversible_and_match_the_schema():
    url = backends.database_url()                       # migrated to head
    assert tables(url) >= {t.name for t in metadata.sorted_tables}
    with backends.engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"include_object": include_object}), metadata)
    assert diff == [], f"schema.py and the migrations disagree: {diff}"

    downgrade(url, "base")
    backends.engine().dispose()
    assert tables(url) == set()
    upgrade(url)                                        # and back up again
    backends.engine().dispose()
    assert tables(url) >= {t.name for t in metadata.sorted_tables}


def test_telemetry_is_a_timescale_hypertable_when_available():
    backends.database_url()
    with backends.engine().connect() as conn:
        has_ts = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).scalar()
        if not has_ts:
            pytest.skip("TimescaleDB extension not installed in this database")
        hyper = conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables")).scalars().all()
        compression = conn.execute(text(
            "SELECT segmentby_column_index IS NOT NULL FROM timescaledb_information.compression_settings "
            "WHERE hypertable_name = 'telemetry_samples' AND attname = 'lap_id'")).scalar()
    assert "telemetry_samples" in hyper
    assert compression


def test_parquet_to_postgres_migration_is_exact_and_idempotent(tmp_path):
    backends.database_url()
    backends.truncate_all()
    source = ParquetRepository(FIXTURE_STORE)
    target = PostgresRepository(backends.engine())
    done = migrate(source, target, log=False)
    assert done == [s.key for s in source.list_sessions()]
    assert migrate(source, target, only_missing=True, log=False) == []        # nothing left to do
    migrate(source, target, log=False)                                        # re-run: replaced, not duplicated
    for info in source.list_sessions():
        key = info.key
        assert target.get_session_info(key) == source.get_session_info(key)
        assert target.list_laps(key) == source.list_laps(key)
        assert target.list_drivers(key) == source.list_drivers(key)
        assert target.get_corners(key) == source.get_corners(key)
        assert target.get_weather(key) == source.get_weather(key)
        stored = [lap for lap in source.list_laps(key) if lap.telemetry_samples]
        for lap in stored[:3]:
            try:
                a = source.get_telemetry(key, lap.driver, lap.lap_number)
            except Exception:
                continue
            b = target.get_telemetry(key, lap.driver, lap.lap_number)
            for name in models.TELEMETRY_COLUMNS:
                np.testing.assert_array_equal(getattr(a, name), getattr(b, name))
    with backends.engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar() == len(done)
