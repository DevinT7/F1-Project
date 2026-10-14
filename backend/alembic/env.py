"""Alembic environment. The database URL comes from the Alembic config (set by app.db.engine)
or RACEIQ_DATABASE_URL, never from alembic.ini (secrets via environment only)."""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.engine import include_object, normalize_url
from app.db.schema import metadata

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url"):
    url = os.environ.get("RACEIQ_DATABASE_URL")
    if not url:
        raise RuntimeError("set RACEIQ_DATABASE_URL (or pass a URL via app.db.engine.upgrade)")
    config.set_main_option("sqlalchemy.url", normalize_url(url).replace("%", "%%"))

target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.",
                                     poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                          include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
