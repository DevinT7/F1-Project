"""Dependency providers for the /api/v1 routes. Tests swap them via app.dependency_overrides."""
from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, Request

from ..ai.llm_client import LLMClient, client_from_env
from ..ai.testing import FAKES
from ..caching import get_cache
from ..jobs.runner import JobSystem
from ..config import settings
from ..ingestion.fastf1_client import FastF1Client
from ..ingestion.service import SessionSource
from ..storage.repository import ParquetRepository, SessionRepository
from ..tires.registry import DbModelRegistry, ModelRegistry, Registry
from ..tires.service import TireService
from .engineer_service import EngineerService
from .services import ComparisonService, SessionQueryService


@lru_cache(maxsize=1)
def get_repository() -> SessionRepository:
    """RACEIQ_STORAGE_BACKEND=parquet (files under RACEIQ_DATA_DIR, the default) or postgres
    (RACEIQ_DATABASE_URL). Both satisfy the same contract (tests/test_repository_contract.py)."""
    if settings.storage_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("RACEIQ_STORAGE_BACKEND=postgres needs RACEIQ_DATABASE_URL")
        from ..db.engine import make_engine
        from ..storage.postgres import PostgresRepository
        return PostgresRepository(make_engine(settings.database_url))
    if settings.storage_backend != "parquet":
        raise RuntimeError(f"unknown RACEIQ_STORAGE_BACKEND {settings.storage_backend!r} (parquet | postgres)")
    return ParquetRepository(settings.data_dir)


def _fastf1_source() -> SessionSource:
    # Constructing the client is cheap; FastF1 itself is only imported on first use.
    return FastF1Client(settings.fastf1_cache_dir)


def get_source_factory() -> Callable[[], SessionSource]:
    """Factory, not instance: read-only routes never construct a FastF1 client."""
    return _fastf1_source


def get_session_service(
    repo: SessionRepository = Depends(get_repository),
    source_factory: Callable[[], SessionSource] = Depends(get_source_factory),
) -> SessionQueryService:
    return SessionQueryService(repo, source_factory, cache=get_cache())


@lru_cache(maxsize=1)
def _comparison_service() -> ComparisonService:
    return ComparisonService(get_repository(), cache=get_cache())


def get_comparison_service() -> ComparisonService:
    """One instance per process so its LRU cache survives across requests."""
    return _comparison_service()


@lru_cache(maxsize=1)
def _tire_service() -> TireService:
    return TireService(get_repository(), get_model_registry())


def get_model_registry() -> Registry:
    """RACEIQ_MODEL_REGISTRY=file (models/registry.json, the default) or db (model_registry table →
    object storage). Both verify artifact checksums before serving."""
    if settings.model_registry == "db":
        if not settings.database_url:
            raise RuntimeError("RACEIQ_MODEL_REGISTRY=db needs RACEIQ_DATABASE_URL")
        from ..db.engine import make_engine
        from ..objectstore import get_object_store
        return DbModelRegistry(make_engine(settings.database_url), get_object_store())
    return ModelRegistry(settings.tire_registry_path)


def get_tire_service() -> TireService:
    """One instance per process: the model, session tables and responses stay cached in memory."""
    return _tire_service()


# ---------------------------------------------------------------- Phase 7F engineer
def get_llm_client() -> LLMClient | None:
    """The LLM used by the engineer: the test-only fake when RACEIQ_ENGINEER_FAKE_LLM is set, else
    the provider from LLM_* env (None without a key: templates are served)."""
    if settings.engineer_fake_llm:
        return _fake_llm(settings.engineer_fake_llm)
    return _provider_client()


@lru_cache(maxsize=1)
def _provider_client() -> LLMClient | None:
    return client_from_env()


@lru_cache(maxsize=4)
def _fake_llm(name: str) -> LLMClient:
    logging.getLogger("raceiq.api").warning("RACEIQ_ENGINEER_FAKE_LLM=%s: a scripted TEST LLM is serving the "
                                            "engineer endpoints; never set this in production", name)
    return FAKES[name]()


def llm_enabled() -> bool:
    return settings.engineer_llm_enabled


@lru_cache(maxsize=1)
def _engineer_service() -> EngineerService:
    return EngineerService(_comparison_service(), _tire_service(), client_factory=lambda: get_llm_client(),
                           llm_enabled=lambda: llm_enabled(), cache=get_cache())


def get_engineer_service() -> EngineerService:
    """One instance per process: its report cache, FactSheet store and conversations live in memory."""
    return _engineer_service()


# ---------------------------------------------------------------- Phase 8 background jobs
def get_job_system() -> JobSystem:
    """RQ + the worker when Redis is configured, else a thread pool in this process (app/jobs/system.py)."""
    from ..jobs.system import get_job_system as system
    return system()
