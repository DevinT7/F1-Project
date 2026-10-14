"""Phase 8 database schema (SQLAlchemy 2.0 Core). The Alembic migrations create exactly this;
tests/test_db_migrations.py checks they agree.

Session data (the SessionRepository's tables):
  sessions          one row per stored session; the manifest columns; info + corners_raw as JSONB
  drivers, laps, corners, weather     children of sessions (ON DELETE CASCADE); `ord` keeps the
                                      stored order; the full model is kept as JSONB next to the
                                      columns used for queries, so a round trip is exact
  telemetry_samples one row per sample (TimescaleDB hypertable on `ts`, compressed by lap_id)
  event_schedules   the per-year schedule cache

Application data (later Phase 8 steps):
  users, analysis_results, saved_analyses, tire_predictions, engineer_reports, ingest_jobs,
  model_registry

Idempotent ingest: sessions.key, (session_id, driver, lap_number), (session_id, code) and
(lap_id, sample_idx, ts) are unique; a re-ingest replaces the session in one transaction.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData,
    String, Table, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

NOW = func.now()


def _session_fk() -> Column[int]:
    return Column("session_id", BigInteger, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)


# ---------------------------------------------------------------- session data
sessions = Table(
    "sessions", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("key", String(16), nullable=False, unique=True),
    Column("year", Integer, nullable=False),
    Column("round", Integer, nullable=False),
    Column("session_code", String(4), nullable=False),
    Column("info", JSONB, nullable=False),
    Column("corners_raw", JSONB, nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    Column("fastf1_version", String(32), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("lap_count", Integer, nullable=False),
    Column("telemetry_count", Integer, nullable=False),
    Column("driver_count", Integer, nullable=False),
    Index(None, "year"),
)

drivers = Table(
    "drivers", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _session_fk(),
    Column("ord", Integer, nullable=False),
    Column("code", String(8), nullable=False),
    Column("number", String(8), nullable=False),
    Column("team", Text),
    Column("data", JSONB, nullable=False),
    UniqueConstraint("session_id", "code"),
)

laps = Table(
    "laps", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _session_fk(),
    Column("session_key", String(16), nullable=False),
    Column("ord", Integer, nullable=False),
    Column("driver", String(8), nullable=False),
    Column("driver_number", String(8), nullable=False),
    Column("lap_number", Integer, nullable=False),
    Column("lap_time_s", Float),
    Column("compound", String(16)),
    Column("stint", Integer),
    Column("is_valid_for_comparison", Boolean, nullable=False),
    Column("telemetry_stored", Boolean, nullable=False, server_default=text("false")),
    Column("data", JSONB, nullable=False),
    UniqueConstraint("session_id", "driver", "lap_number"),
    Index(None, "session_key", "driver", "lap_number"),
)

TELEMETRY_FLOATS = ("time_s", "distance_m", "speed_kph", "throttle_pct", "rpm", "x", "y")
telemetry_samples = Table(
    "telemetry_samples", metadata,
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("lap_id", BigInteger, ForeignKey("laps.id", ondelete="CASCADE"), nullable=False),
    Column("sample_idx", Integer, nullable=False),
    *[Column(name, Float(precision=53), nullable=False) for name in ("time_s", "distance_m", "speed_kph", "throttle_pct")],
    Column("brake", Boolean, nullable=False),
    Column("gear", Integer, nullable=False),
    Column("rpm", Float(precision=53), nullable=False),
    Column("drs_code", Integer, nullable=False),
    Column("drs_open", Boolean, nullable=False),
    Column("x", Float(precision=53), nullable=False),
    Column("y", Float(precision=53), nullable=False),
    UniqueConstraint("lap_id", "sample_idx", "ts"),
    Index(None, "lap_id", "sample_idx"),
)

corners = Table(
    "corners", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _session_fk(),
    Column("ord", Integer, nullable=False),
    Column("number", Integer, nullable=False),
    Column("letter", String(4)),
    Column("distance_m", Float(precision=53), nullable=False),
    Column("x", Float(precision=53), nullable=False),
    Column("y", Float(precision=53), nullable=False),
    Column("angle", Float(precision=53), nullable=False),
    UniqueConstraint("session_id", "ord"),
)

weather = Table(
    "weather", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    _session_fk(),
    Column("ord", Integer, nullable=False),
    Column("time_s", Float(precision=53), nullable=False),
    Column("air_temp_c", Float(precision=53)),
    Column("track_temp_c", Float(precision=53)),
    Column("humidity", Float(precision=53)),
    Column("rainfall", Boolean),
    Column("wind_kph", Float(precision=53)),
    UniqueConstraint("session_id", "ord"),
)

event_schedules = Table(
    "event_schedules", metadata,
    Column("year", Integer, primary_key=True, autoincrement=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("events", JSONB, nullable=False),
)

# ---------------------------------------------------------------- application data
users = Table(
    "users", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("role", String(16), nullable=False, server_default=text("'user'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("token_version", Integer, nullable=False, server_default=text("0")),
    CheckConstraint("role in ('user', 'admin')", name="role"),
)

analysis_results = Table(
    "analysis_results", metadata,
    Column("params_hash", String(64), primary_key=True),
    Column("session_key", String(16), nullable=False),
    Column("params", JSONB, nullable=False),
    Column("findings", JSONB, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Index(None, "session_key"),
)

saved_analyses = Table(
    "saved_analyses", metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("title", String(200), nullable=False),
    Column("params", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Index(None, "user_id", "created_at"),
)

tire_predictions = Table(
    "tire_predictions", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("session_key", String(16), nullable=False),
    Column("driver", String(8), nullable=False),
    Column("lap", Integer, nullable=False),
    Column("horizon", Integer, nullable=False),
    Column("model_version", String(64), nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    UniqueConstraint("session_key", "driver", "lap", "horizon", "model_version"),
)

engineer_reports = Table(
    "engineer_reports", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("factsheet_hash", String(64), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    Column("model", String(128), nullable=False),
    Column("generated_by", String(16), nullable=False),
    Column("output", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    UniqueConstraint("factsheet_hash", "prompt_version", "model"),
)

ingest_jobs = Table(
    "ingest_jobs", metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),            # ingest | train
    Column("target", String(64), nullable=False),          # session key / model name
    Column("status", String(16), nullable=False),          # queued | running | succeeded | failed
    Column("progress", Float, nullable=False, server_default=text("0")),
    Column("message", Text),
    Column("error", Text),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("created_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    Index(None, "status", "created_at"),
)

model_registry = Table(
    "model_registry", metadata,
    Column("name", String(64), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),          # production | candidate | retired
    Column("object_key", Text, nullable=False),            # S3 key of the artifact
    Column("sha256", String(64), nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=NOW),
    CheckConstraint("status in ('production', 'candidate', 'retired')", name="status"),
)

SESSION_TABLES = (sessions, drivers, laps, telemetry_samples, corners, weather)
