"""Engine creation and Alembic helpers (the migrations live in backend/alembic/)."""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

BACKEND = Path(__file__).resolve().parents[2]


def normalize_url(url: str) -> str:
    """Use psycopg 3 for plain postgres URLs (postgres:// and postgresql:// default to psycopg2)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url.replace("postgresql+psycopg2://", "postgresql+psycopg://")


def make_engine(url: str, **kwargs: object) -> Engine:
    return create_engine(normalize_url(url), pool_pre_ping=True, future=True, **kwargs)


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", normalize_url(url).replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """Autogenerate / schema comparison filter: TimescaleDB creates its own time index on a
    hypertable (telemetry_samples_ts_idx); it is not part of schema.py."""
    return not (type_ == "index" and reflected and compare_to is None and (name or "").endswith("_ts_idx"))
