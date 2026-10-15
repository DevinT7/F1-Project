"""Phase 8 application cache: one Cache protocol, in-memory and Redis implementations.

    cache = get_cache()                                   # Redis when RACEIQ_REDIS_URL is set
    value, hit = single_flight(cache, cache_key("cmp", session, digest), compute, TTL_S["cmp"])

Keys are namespaced and versioned: raceiq:v1:{kind}:{session or scope}:{digest}. The digest of
a comparison includes the storage SCHEMA_VERSION, the findings schema version and the session's
ingested_at, so a re-ingest or a schema bump can never serve stale data; ingest also deletes
the session's prefixes (invalidate_session) to free the space at once.

  kind    what                                   TTL
  cmp     comparison payload (JSON)              24 h
  fs      FactSheet for a comparison (+ tyres)   24 h
  fsh     FactSheet by hash (GET /engineer/facts) 24 h
  rpt     engineer report (+ prompt, model)       7 d
  sched   season / event lists                    6 h

single_flight: the first caller takes a short lock (SET NX with a TTL), computes and stores;
concurrent callers wait for the value instead of computing it again. If the leader fails, the
lock is released and one waiter takes over. If Redis is unreachable, the cache degrades to
"compute every time" (logged) instead of failing requests.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Protocol

import structlog

log = structlog.get_logger("raceiq.cache")

NAMESPACE = "raceiq:v1"
TTL_S = {"cmp": 24 * 3600, "fs": 24 * 3600, "fsh": 24 * 3600, "rpt": 7 * 24 * 3600, "sched": 6 * 3600}
LOCK_TTL_S = 120
WAIT_S = 60.0
POLL_S = 0.02


class Cache(Protocol):
    name: str

    def get(self, key: str) -> bytes | None: ...
    def set(self, key: str, value: bytes, ttl_s: int) -> None: ...
    def add(self, key: str, value: bytes, ttl_s: int) -> bool: ...          # set only if absent
    def delete(self, key: str) -> None: ...
    def delete_if(self, key: str, value: bytes) -> None: ...                 # compare-and-delete (locks)
    def delete_prefix(self, prefix: str) -> int: ...
    def ping(self) -> bool: ...


def cache_key(kind: str, scope: str, digest: str) -> str:
    return f"{NAMESPACE}:{kind}:{scope}:{digest}"


def digest(*parts: Any) -> str:
    """Stable hash of the parameters that define a cached value (JSON-canonical)."""
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------- in-memory
class MemoryCache:
    """Process-local LRU with TTLs (the default without Redis; per process, lost on restart)."""
    name = "memory"

    def __init__(self, max_entries: int = 512, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries = max_entries
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def _live(self, key: str) -> bytes | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        if hit[0] <= self._clock():
            del self._data[key]
            return None
        return hit[1]

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._live(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def set(self, key: str, value: bytes, ttl_s: int) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl_s, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def add(self, key: str, value: bytes, ttl_s: int) -> bool:
        with self._lock:
            if self._live(key) is not None:
                return False
            self._data[key] = (self._clock() + ttl_s, value)
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_if(self, key: str, value: bytes) -> None:
        with self._lock:
            if self._live(key) == value:
                del self._data[key]

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def ping(self) -> bool:
        return True


# ---------------------------------------------------------------- redis
_COMPARE_AND_DELETE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisCache:
    """Redis-backed; shared by every worker. Connection errors degrade to "no cache" (logged)."""
    name = "redis"

    def __init__(self, url: str, client: Any | None = None) -> None:
        import redis
        self._errors: tuple[type[BaseException], ...] = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
        self.client = client or redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
        self._cad = self.client.register_script(_COMPARE_AND_DELETE)

    def _guard(self, what: str, default: Any, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except self._errors as exc:
            log.warning("cache.redis_unavailable", op=what, error=type(exc).__name__)
            return default

    def get(self, key: str) -> bytes | None:
        value = self._guard("get", None, lambda: self.client.get(key))
        return value if isinstance(value, bytes) else None

    def set(self, key: str, value: bytes, ttl_s: int) -> None:
        self._guard("set", None, lambda: self.client.set(key, value, ex=ttl_s))

    def add(self, key: str, value: bytes, ttl_s: int) -> bool:
        # Redis down: "acquired", so the caller computes instead of waiting for nobody
        return bool(self._guard("add", True, lambda: self.client.set(key, value, ex=ttl_s, nx=True)))

    def delete(self, key: str) -> None:
        self._guard("delete", None, lambda: self.client.delete(key))

    def delete_if(self, key: str, value: bytes) -> None:
        self._guard("delete_if", None, lambda: self._cad(keys=[key], args=[value]))

    def delete_prefix(self, prefix: str) -> int:
        def run() -> int:
            n = 0
            batch: list[bytes] = []
            for k in self.client.scan_iter(match=prefix.replace("[", "\\[") + "*", count=500):
                batch.append(k)
                if len(batch) >= 500:
                    n += int(self.client.unlink(*batch))
                    batch = []
            if batch:
                n += int(self.client.unlink(*batch))
            return n
        return int(self._guard("delete_prefix", 0, run))

    def ping(self) -> bool:
        return bool(self._guard("ping", False, lambda: self.client.ping()))


# ---------------------------------------------------------------- single flight
class FlightStats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.computed: dict[str, int] = {}
        self.hits: dict[str, int] = {}
        self.misses: dict[str, int] = {}

    def bump(self, table: dict[str, int], kind: str) -> None:
        with self.lock:
            table[kind] = table.get(kind, 0) + 1
        if table is self.hits or table is self.misses:     # → raceiq_cache_lookups_total (hit ratio)
            from .observability import CACHE_LOOKUPS
            CACHE_LOOKUPS.labels(kind, "hit" if table is self.hits else "miss").inc()


STATS = FlightStats()


def _kind(key: str) -> str:
    return key[len(NAMESPACE) + 1:].split(":", 1)[0] if key.startswith(NAMESPACE) else "other"


def single_flight(cache: Cache, key: str, compute: Callable[[], bytes], ttl_s: int, *,
                  lock_ttl_s: int = LOCK_TTL_S, wait_s: float = WAIT_S,
                  ttl_for: Callable[[bytes], int] | None = None) -> tuple[bytes, bool]:
    """(value, was_cached). Identical concurrent callers compute once. `ttl_for(value)` may
    override the TTL per value; 0 means "do not store" (e.g. an LLM report that fell back)."""
    kind = _kind(key)
    value = cache.get(key)
    if value is not None:
        STATS.bump(STATS.hits, kind)
        return value, True
    STATS.bump(STATS.misses, kind)
    lock = f"{key}:lock"
    deadline = time.monotonic() + wait_s
    while True:
        token = uuid.uuid4().hex.encode()
        if cache.add(lock, token, lock_ttl_s):
            try:
                value = cache.get(key)                 # someone finished between our miss and the lock
                if value is not None:
                    return value, True
                value = compute()
                STATS.bump(STATS.computed, kind)
                ttl = ttl_s if ttl_for is None else ttl_for(value)
                if ttl > 0:
                    cache.set(key, value, ttl)
                return value, False
            finally:
                cache.delete_if(lock, token)
        while time.monotonic() < deadline:             # someone else is computing: wait for it
            time.sleep(POLL_S)
            value = cache.get(key)
            if value is not None:
                STATS.bump(STATS.hits, kind)
                return value, True
            if cache.get(lock) is None:                # the leader gave up (error): take over
                break
        else:
            log.warning("cache.single_flight_timeout", key=key)
            value = compute()                          # never wait forever
            STATS.bump(STATS.computed, kind)
            return value, False


def invalidate_session(cache: Cache, session_key: str) -> int:
    """Drop everything cached for a session (called after a re-ingest)."""
    return sum(cache.delete_prefix(f"{NAMESPACE}:{kind}:{session_key}:") for kind in ("cmp", "fs"))


@lru_cache(maxsize=1)
def get_cache() -> Cache:
    from .config import settings
    url = settings.redis_url
    if url:
        log.info("cache.backend", backend="redis")
        return RedisCache(url)
    return MemoryCache()
