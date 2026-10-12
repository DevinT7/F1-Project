"""Dependency providers for the /api/v1 routes. Tests swap them via app.dependency_overrides."""
from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from fastapi import Depends

from ..config import settings
from ..ingestion.fastf1_client import FastF1Client
from ..ingestion.service import SessionSource
from ..storage.repository import ParquetRepository, SessionRepository
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
