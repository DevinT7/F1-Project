"""Phase 8 — the app cache: both implementations, versioned keys, single flight, invalidation.

Redis comes from RACEIQ_TEST_REDIS_URL, else a testcontainers Redis when Docker runs, else the
redis cases are skipped with the reason.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from app.api.schemas.comparisons import ComparisonRequest
from app.api.services import ComparisonService, SessionQueryService
from app.caching import (
    NAMESPACE, TTL_S, MemoryCache, RedisCache, cache_key, digest, invalidate_session, single_flight,
)
from app.storage.repository import ParquetRepository, load_bundle
from tests.repository_backends import _docker_running

FIXTURE_STORE = Path(__file__).parent / "fixtures" / "ingestion" / "store"
_REDIS: dict[str, object] = {}


def redis_url() -> str:
    if "url" in _REDIS:
        return str(_REDIS["url"])
    url = os.environ.get("RACEIQ_TEST_REDIS_URL")
    if not url:
        if not _docker_running():
            pytest.skip("Redis backend skipped: set RACEIQ_TEST_REDIS_URL or start Docker (testcontainers)")
        from testcontainers.redis import RedisContainer
        container = RedisContainer("redis:7-alpine")
        container.start()
        _REDIS["container"] = container
        url = f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"
    _REDIS["url"] = url
    return url


@pytest.fixture(scope="module", autouse=True)
def _stop_redis():
    yield
    container = _REDIS.pop("container", None)
    if container is not None:
        container.stop()  # type: ignore[attr-defined]
    _REDIS.clear()


@pytest.fixture(params=["memory", "redis"])
def cache(request):
    if request.param == "memory":
        yield MemoryCache()
        return
    c = RedisCache(redis_url())
    c.client.flushdb()
    yield c


def body(tgt: str = "ALO") -> ComparisonRequest:
    return ComparisonRequest.model_validate({"session_key": "2024-05-Q", "reference": {"driver": "VER", "lap": "fastest"},
                                             "target": {"driver": tgt, "lap": "fastest"}})


# ---------------------------------------------------------------- the protocol
def test_get_set_delete_and_add(cache):
    assert cache.get("k") is None
    cache.set("k", b"v", 60)
    assert cache.get("k") == b"v"
    assert cache.add("k", b"other", 60) is False and cache.get("k") == b"v"
    assert cache.add("k2", b"x", 60) is True
    cache.delete("k")
    assert cache.get("k") is None
    cache.delete_if("k2", b"not-mine")
    assert cache.get("k2") == b"x"
    cache.delete_if("k2", b"x")
    assert cache.get("k2") is None
    assert cache.ping()


def test_ttl_expires(cache):
    if isinstance(cache, MemoryCache):
        now = [1000.0]
        cache = MemoryCache(clock=lambda: now[0])
        cache.set("k", b"v", 5)
        now[0] += 4.9
        assert cache.get("k") == b"v"
        now[0] += 0.2
        assert cache.get("k") is None
    else:
        cache.set("k", b"v", 1)
        assert 0 < cache.client.ttl("k") <= 1
        deadline = time.monotonic() + 3
        while cache.get("k") is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert cache.get("k") is None


def test_delete_prefix_only_touches_that_prefix(cache):
    for i in range(7):
        cache.set(cache_key("cmp", "2024_05_Q", f"d{i}"), b"x", 60)
    cache.set(cache_key("cmp", "2024_06_Q", "d0"), b"y", 60)
    cache.set(cache_key("fs", "2024_05_Q", "d0"), b"z", 60)
    assert invalidate_session(cache, "2024_05_Q") == 8
    assert cache.get(cache_key("cmp", "2024_06_Q", "d0")) == b"y"


def test_keys_are_namespaced_and_versioned():
    k = cache_key("cmp", "2024_05_Q", digest(1, 1, "2024-04-20T07:00:00", "VER", 17))
    assert k.startswith(f"{NAMESPACE}:cmp:2024_05_Q:") and NAMESPACE == "raceiq:v1"
    # every version that defines the value changes the key
    assert digest(1, "report-v1", "model-a", "h") != digest(1, "report-v2", "model-a", "h")
    assert digest(1, "report-v1", "model-a", "h") != digest(1, "report-v1", "model-b", "h")
    assert digest(1, "x") != digest(2, "x")
    assert set(TTL_S) == {"cmp", "fs", "fsh", "rpt", "sched"}


# ---------------------------------------------------------------- single flight
def test_identical_concurrent_requests_compute_once(cache):
    calls = []
    gate = threading.Event()

    def compute() -> bytes:
        calls.append(1)
        gate.wait(0.3)                                   # long enough for everyone to pile up
        return b"result"

    results: list[tuple[bytes, bool]] = []
    threads = [threading.Thread(target=lambda: results.append(single_flight(cache, "raceiq:v1:cmp:s:d", compute, 60)))
               for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert len(results) == 16 and all(v == b"result" for v, _ in results)
    assert sum(1 for _, hit in results if not hit) == 1


def test_a_failed_leader_hands_over_to_a_waiter(cache):
    attempts = []

    def flaky() -> bytes:
        attempts.append(1)
        if len(attempts) == 1:
            time.sleep(0.1)
            raise RuntimeError("boom")
        return b"ok"

    out: list[object] = []

    def run() -> None:
        try:
            out.append(single_flight(cache, "raceiq:v1:cmp:s:f", flaky, 60)[0])
        except RuntimeError as exc:
            out.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(o, RuntimeError) for o in out) == 1
    assert out.count(b"ok") == 3 and len(attempts) == 2


def test_ttl_for_zero_is_not_stored(cache):
    value, hit = single_flight(cache, "raceiq:v1:rpt:s:x", lambda: b"fallback", 60, ttl_for=lambda v: 0)
    assert value == b"fallback" and not hit and cache.get("raceiq:v1:rpt:s:x") is None


def test_unreachable_redis_degrades_to_computing():
    dead = RedisCache("redis://127.0.0.1:1/0")
    assert dead.get("k") is None and dead.add("k", b"v", 5) is True and dead.ping() is False
    assert single_flight(dead, "raceiq:v1:cmp:s:z", lambda: b"computed", 60) == (b"computed", False)


# ---------------------------------------------------------------- comparisons
def test_comparison_hit_miss_and_invalidation_on_reingest(cache, tmp_path):
    store = tmp_path / "store"
    shutil.copytree(FIXTURE_STORE / "2024_05_Q", store / "2024_05_Q")
    repo = ParquetRepository(store)
    svc = ComparisonService(repo, cache=cache)
    first = svc.compare(body())
    assert first.meta.cached is False
    assert svc.compare(body()).meta.cached is True
    assert (svc.hits, svc.misses) == (1, 1)
    # re-ingest: a new ingested_at → a different key (never stale), and the old prefix is dropped
    repo.save_session(load_bundle(repo, "2024_05_Q"))
    svc2 = ComparisonService(repo, cache=cache)
    assert svc2.compare(body()).meta.cached is False
    assert invalidate_session(cache, "2024_05_Q") >= 1
    assert svc2.compare(body()).meta.cached is False


def test_concurrent_identical_comparisons_compute_once_even_across_workers(cache, monkeypatch):
    built = []
    real = ComparisonService._build

    def counting(self, *args, **kwargs):
        built.append(1)
        time.sleep(0.2)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonService, "_build", counting)
    repo = ParquetRepository(FIXTURE_STORE)
    workers = [ComparisonService(repo, cache=cache) for _ in range(3)]      # e.g. three uvicorn workers
    out: list[str] = []
    threads = [threading.Thread(target=lambda w=w: out.append(w.compare_json(body())))
               for w in workers for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1          # memory: one shared process-local cache; redis: shared by the "workers"
    assert len(out) == 12 and len({o.replace('"cached":true', '"cached":false') for o in out}) == 1


def test_event_schedules_are_cached(cache, tmp_path):
    repo = ParquetRepository(tmp_path / "data")
    calls = []

    class Source:
        def list_events(self, year):
            calls.append(year)
            return []

    svc = SessionQueryService(repo, lambda: Source(), cache=cache)
    svc._ingestion = lambda: Source()  # type: ignore[method-assign]
    assert svc.events(2024).year == 2024
    assert svc.events(2024).events == []
    assert calls == [2024]
