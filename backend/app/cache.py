"""
Tiny async cache. In-memory dict for dev; swap to Redis in prod by setting
RACEIQ_REDIS_URL. Historical F1 data is immutable, so we cache hard.

Person 1 owns this. Kept intentionally dependency-light.
"""
from __future__ import annotations

import time
from typing import Any

from .config import settings

_store: dict[str, tuple[float, Any]] = {}


def get(key: str) -> Any | None:
    hit = _store.get(key)
    if hit is None:
        return None
    expires_at, value = hit
    if expires_at < time.time():
        _store.pop(key, None)
        return None
    return value


def set(key: str, value: Any, ttl: int | None = None) -> None:
    ttl = settings.cache_ttl_seconds if ttl is None else ttl
    _store[key] = (time.time() + ttl, value)


def clear() -> None:
    _store.clear()
