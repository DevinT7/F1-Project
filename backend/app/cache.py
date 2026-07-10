"""Cache abstraction for local development and Redis-backed deployments."""
from __future__ import annotations

import os
import pickle
import time
from typing import Any, Protocol

from .config import settings


class CacheBackend(Protocol):
    name: str

    def get(self, key: str) -> Any | None:
        ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ...

    def delete(self, key: str) -> bool:
        ...

    def clear(self) -> None:
        ...

    def status(self) -> str:
        ...


def _default_ttl_seconds() -> int:
    raw = os.getenv("CACHE_DEFAULT_TTL_SECONDS") or os.getenv("RACEIQ_CACHE_TTL_SECONDS")
    if raw is None:
        return settings.cache_ttl_seconds
    try:
        return int(raw)
    except ValueError:
        return settings.cache_ttl_seconds


def _redis_url() -> str | None:
    return os.getenv("REDIS_URL") or os.getenv("RACEIQ_REDIS_URL") or settings.redis_url


class InMemoryCache:
    name = "memory"

    def __init__(self, default_ttl_seconds: int) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self._store: dict[str, tuple[float | None, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if expires_at is not None and expires_at <= time.time():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl_seconds = self.default_ttl_seconds if ttl is None else ttl
        expires_at = None if ttl_seconds <= 0 else time.time() + ttl_seconds
        self._store[key] = (expires_at, value)

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        self._store.clear()

    def status(self) -> str:
        return "ok"


class RedisCache:
    name = "redis"

    def __init__(self, url: str, default_ttl_seconds: int, namespace: str = "raceiq") -> None:
        self.url = url
        self.default_ttl_seconds = default_ttl_seconds
        self.namespace = namespace
        self._client: Any | None = None
        self._last_error: Exception | None = None

    def _connect(self) -> Any:
        if self._client is None:
            try:
                import redis

                self._client = redis.Redis.from_url(self.url)
            except Exception as exc:  # pragma: no cover - depends on optional Redis runtime
                self._last_error = exc
                raise
        return self._client

    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str) -> Any | None:
        try:
            value = self._connect().get(self._key(key))
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            self._last_error = exc
            return None
        if value is None:
            return None
        try:
            return pickle.loads(value)
        except (pickle.PickleError, TypeError, EOFError):
            self.delete(key)
            return None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl_seconds = self.default_ttl_seconds if ttl is None else ttl
        expires_in = None if ttl_seconds <= 0 else ttl_seconds
        try:
            self._connect().set(self._key(key), pickle.dumps(value), ex=expires_in)
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            self._last_error = exc

    def delete(self, key: str) -> bool:
        try:
            return bool(self._connect().delete(self._key(key)))
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            self._last_error = exc
            return False

    def clear(self) -> None:
        try:
            client = self._connect()
            keys = list(client.scan_iter(match=f"{self.namespace}:*"))
            if keys:
                client.delete(*keys)
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            self._last_error = exc

    def status(self) -> str:
        try:
            return "ok" if self._connect().ping() else "error"
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            self._last_error = exc
            return "error"


def _build_cache() -> CacheBackend:
    default_ttl = _default_ttl_seconds()
    redis_url = _redis_url()
    if redis_url:
        return RedisCache(redis_url, default_ttl)
    return InMemoryCache(default_ttl)


_cache = _build_cache()


def get(key: str) -> Any | None:
    return _cache.get(key)


def set(key: str, value: Any, ttl: int | None = None) -> None:
    _cache.set(key, value, ttl)


def delete(key: str) -> bool:
    return _cache.delete(key)


def clear() -> None:
    _cache.clear()


def status() -> str:
    return _cache.status()


def backend_name() -> str:
    return _cache.name
