"""Phase 8 rate limits: token buckets per client, in Redis (shared by every worker) or in memory.

  policy     key                 capacity   refill            where
  api        IP                  120        2 / s  (120/min)  every /api/v1 request (middleware)
  auth       IP                  10         10 / min          register, login, refresh
  engineer   user                20         20 / min          /engineer/report and /ask (LLM cost)
  admin      user                10         10 / min          ingest and training jobs

A request that finds its bucket empty gets 429 with `Retry-After` (whole seconds until a token
is back). Redis errors fail OPEN (logged): an outage of the limiter must not take the API down.
Client IP is request.client.host; X-Forwarded-For is used only when RACEIQ_TRUST_PROXY=true.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import structlog

log = structlog.get_logger("raceiq.ratelimit")


@dataclass(frozen=True)
class Policy:
    name: str
    capacity: int
    refill_per_s: float


POLICIES = {
    "api": Policy("api", 120, 2.0),
    "auth": Policy("auth", 10, 10 / 60),
    "engineer": Policy("engineer", 20, 20 / 60),
    "admin": Policy("admin", 10, 10 / 60),
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: int = 0
    remaining: int = 0


class Limiter(Protocol):
    def hit(self, policy: Policy, client: str, cost: float = 1.0) -> Decision: ...


class MemoryLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}      # key → (tokens, updated)
        self._lock = threading.Lock()

    def hit(self, policy: Policy, client: str, cost: float = 1.0) -> Decision:
        key = f"{policy.name}:{client}"
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(policy.capacity), now))
            tokens = min(float(policy.capacity), tokens + (now - updated) * policy.refill_per_s)
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                return Decision(True, 0, int(tokens - cost))
            self._buckets[key] = (tokens, now)
            return Decision(False, max(1, math.ceil((cost - tokens) / policy.refill_per_s)), 0)


# KEYS[1] bucket; ARGV: capacity, refill/s, now (s), cost → {allowed, retry_after, remaining}
_LUA = """
local cap = tonumber(ARGV[1]); local rate = tonumber(ARGV[2]); local now = tonumber(ARGV[3]); local cost = tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or cap
local ts = tonumber(b[2]) or now
tokens = math.min(cap, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 else retry = math.ceil((cost - tokens) / rate) end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) + 60)
return {allowed, retry, math.floor(tokens)}
"""


class RedisLimiter:
    def __init__(self, client: Any, prefix: str = "raceiq:v1:rl") -> None:
        import redis
        self.client = client
        self.prefix = prefix
        self._script = client.register_script(_LUA)
        self._errors: tuple[type[BaseException], ...] = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)

    def hit(self, policy: Policy, client: str, cost: float = 1.0) -> Decision:
        try:
            allowed, retry, remaining = self._script(keys=[f"{self.prefix}:{policy.name}:{client}"],
                                                     args=[policy.capacity, policy.refill_per_s, time.time(), cost])
        except self._errors as exc:
            log.warning("ratelimit.redis_unavailable", error=type(exc).__name__)
            return Decision(True)                          # fail open
        return Decision(bool(allowed), max(1, int(retry)) if not allowed else 0, int(remaining))


@lru_cache(maxsize=1)
def get_limiter() -> Limiter:
    from .config import settings
    if settings.redis_url:
        import redis
        return RedisLimiter(redis.Redis.from_url(settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0))
    return MemoryLimiter()


def client_ip(request: Any) -> str:
    from .config import settings
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return str(forwarded.split(",")[0].strip())
    return str(request.client.host) if request.client else "unknown"


class RateLimited(Exception):
    def __init__(self, decision: Decision, policy: str) -> None:
        super().__init__(f"rate limit '{policy}' exceeded; retry in {decision.retry_after_s} s")
        self.decision = decision
        self.policy = policy
