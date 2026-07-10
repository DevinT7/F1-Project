from __future__ import annotations

import asyncio

import httpx
import pytest

from app.cache import InMemoryCache
from app.clients.openf1 import (
    OpenF1Client,
    ProviderBadResponseError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)


class _NoopRateLimiter:
    async def wait(self) -> None:
        pass


def _run(coro):
    return asyncio.run(coro)


def _client(handler, cache_backend: InMemoryCache | None = None) -> OpenF1Client:
    return OpenF1Client(
        base_url="https://example.test/v1",
        cache_backend=cache_backend or InMemoryCache(default_ttl_seconds=60),
        max_retries=0,
        rate_limiter=_NoopRateLimiter(),
        transport=httpx.MockTransport(handler),
    )


def test_successful_sessions_request_uses_openf1_shape() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"session_key": 1, "year": 2024, "session_type": "Race"}],
            request=request,
        )

    client = _client(handler)

    async def call():
        try:
            return await client.get_sessions(year=2024)
        finally:
            await client.aclose()

    result = _run(call())

    assert result.data == [{"session_key": 1, "year": 2024, "session_type": "Race"}]
    assert result.cached is False
    assert result.source == "openf1"
    assert result.meta["count"] == 1
    assert seen[0].url.path == "/v1/sessions"
    assert "year=2024" in str(seen[0].url)


def test_provider_timeout_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    client = _client(handler)

    async def call():
        try:
            await client.get_sessions(year=2024)
        finally:
            await client.aclose()

    with pytest.raises(ProviderTimeoutError):
        _run(call())


def test_provider_429_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"}, request=request)

    client = _client(handler)

    async def call():
        try:
            await client.get_sessions(year=2024)
        finally:
            await client.aclose()

    with pytest.raises(ProviderRateLimitError):
        _run(call())


def test_malformed_provider_response_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"}, request=request)

    client = _client(handler)

    async def call():
        try:
            await client.get_sessions(year=2024)
        finally:
            await client.aclose()

    with pytest.raises(ProviderBadResponseError):
        _run(call())


def test_cache_miss_fetches_provider() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[{"session_key": 1}], request=request)

    client = _client(handler)

    async def call():
        try:
            return await client.get_sessions(year=2024)
        finally:
            await client.aclose()

    result = _run(call())

    assert calls == 1
    assert result.cached is False
    assert result.source == "openf1"


def test_cache_hit_skips_provider() -> None:
    calls = 0
    cache = InMemoryCache(default_ttl_seconds=60)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[{"session_key": 1}], request=request)

    client = _client(handler, cache_backend=cache)

    async def call_twice():
        try:
            first = await client.get_sessions(year=2024)
            second = await client.get_sessions(year=2024)
            return first, second
        finally:
            await client.aclose()

    first, second = _run(call_twice())

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.source == "cache"
