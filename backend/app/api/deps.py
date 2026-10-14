"""Dependency providers for the /api/v1 routes. Tests swap them via app.dependency_overrides."""
from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends, Request

from ..ai.llm_client import LLMClient, client_from_env
from ..ai.testing import FAKES
from ..config import settings
from ..ingestion.fastf1_client import FastF1Client
from ..ingestion.service import SessionSource
from ..storage.repository import ParquetRepository, SessionRepository
from ..tires.registry import ModelRegistry
from ..tires.service import TireService
from .engineer_service import EngineerService
from .services import ComparisonService, SessionQueryService


@lru_cache(maxsize=1)
def get_repository() -> SessionRepository:
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
    return SessionQueryService(repo, source_factory)


@lru_cache(maxsize=1)
def _comparison_service() -> ComparisonService:
    return ComparisonService(get_repository())


def get_comparison_service() -> ComparisonService:
    """One instance per process so its LRU cache survives across requests."""
    return _comparison_service()


@lru_cache(maxsize=1)
def _tire_service() -> TireService:
    return TireService(get_repository(), ModelRegistry(settings.tire_registry_path))


def get_tire_service() -> TireService:
    """One instance per process: the model, session tables and responses stay cached in memory."""
    return _tire_service()


# ---------------------------------------------------------------- Phase 7F engineer
def engineer_rate_limit(request: Request) -> None:
    """Rate-limit hook for /api/v1/engineer (stub: allows everything). Phase 8 enforces a
    per-client token bucket here (LLM calls cost money); the global limiter stays in errors.py."""
    return None


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
                           llm_enabled=lambda: llm_enabled())


def get_engineer_service() -> EngineerService:
    """One instance per process: its report cache, FactSheet store and conversations live in memory."""
    return _engineer_service()
