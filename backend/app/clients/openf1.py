"""OpenF1 REST client and provider-facing safeguards."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .. import cache
from ..config import settings

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMIT_PER_SECOND = 3
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
DEFAULT_MAX_RETRIES = 2
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class OpenF1Error(RuntimeError):
    """Base class for normalized OpenF1 provider errors."""


class ProviderTimeoutError(OpenF1Error):
    """OpenF1 did not respond before the configured timeout."""


class ProviderRateLimitError(OpenF1Error):
    """OpenF1 returned a rate-limit response."""


class ProviderUnavailableError(OpenF1Error):
    """OpenF1 is temporarily unavailable or unreachable."""


class ProviderBadResponseError(OpenF1Error):
    """OpenF1 returned a malformed or unexpected response."""


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None:
        ...

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ...


class RateLimiter(Protocol):
    async def wait(self) -> None:
        ...


@dataclass(frozen=True)
class OpenF1Result:
    data: list[dict[str, Any]]
    cached: bool
    cache_ttl_seconds: int
    endpoint: str

    @property
    def source(self) -> str:
        return "cache" if self.cached else "openf1"

    @property
    def meta(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "cached": self.cached,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "count": len(self.data),
        }


class AsyncOpenF1RateLimiter:
    """Cooperative in-process limiter for OpenF1's free-tier request limits."""

    def __init__(
        self,
        *,
        per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND,
        per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self._second_window: deque[float] = deque()
        self._minute_window: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                if (
                    len(self._second_window) < self.per_second
                    and len(self._minute_window) < self.per_minute
                ):
                    self._second_window.append(now)
                    self._minute_window.append(now)
                    return
                wait_s = self._next_wait_seconds(now)
            await asyncio.sleep(wait_s)

    def _prune(self, now: float) -> None:
        while self._second_window and now - self._second_window[0] >= 1.0:
            self._second_window.popleft()
        while self._minute_window and now - self._minute_window[0] >= 60.0:
            self._minute_window.popleft()

    def _next_wait_seconds(self, now: float) -> float:
        waits: list[float] = []
        if len(self._second_window) >= self.per_second:
            waits.append(1.0 - (now - self._second_window[0]))
        if len(self._minute_window) >= self.per_minute:
            waits.append(60.0 - (now - self._minute_window[0]))
        return max(0.01, min(waits or [0.01]))


_shared_rate_limiter = AsyncOpenF1RateLimiter()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name) or os.getenv(f"RACEIQ_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer env var %s=%r", name, raw)
        return default


def _cache_ttls() -> dict[str, int]:
    return {
        "sessions": _env_int("OPENF1_SESSIONS_CACHE_TTL_SECONDS", 24 * 60 * 60),
        "drivers": _env_int("OPENF1_DRIVERS_CACHE_TTL_SECONDS", 6 * 60 * 60),
        "laps": _env_int("OPENF1_LAPS_CACHE_TTL_SECONDS", 60 * 60),
        "stints": _env_int("OPENF1_STINTS_CACHE_TTL_SECONDS", 60 * 60),
        "weather": _env_int("OPENF1_WEATHER_CACHE_TTL_SECONDS", 30 * 60),
    }


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (params or {}).items() if value is not None}


def _cache_key(endpoint: str, params: dict[str, Any], extra: str | None = None) -> str:
    return f"openf1:{endpoint}:{extra or ''}:{sorted(params.items())}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class OpenF1Client:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        cache_backend: CacheStore | None = None,
        max_retries: int | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.openf1_base_url).rstrip("/")
        self.cache = cache_backend or cache
        self.max_retries = DEFAULT_MAX_RETRIES if max_retries is None else max_retries
        self.rate_limiter = rate_limiter or _shared_rate_limiter
        self.cache_ttls = _cache_ttls()

        headers = {}
        if settings.openf1_api_key:
            headers["Authorization"] = f"Bearer {settings.openf1_api_key}"

        timeout = httpx.Timeout(timeout_s or settings.request_timeout_s)
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_sessions(
        self,
        year: int | None = None,
        country_name: str | None = None,
        session_type: str | None = None,
        *,
        session_name: str | None = None,
        session_key: int | str | None = None,
    ) -> OpenF1Result:
        return await self._get(
            "sessions",
            {
                "year": year,
                "country_name": country_name,
                "session_type": session_type,
                "session_name": session_name,
                "session_key": session_key,
            },
            cache_ttl_seconds=self.cache_ttls["sessions"],
        )

    async def get_drivers(
        self,
        session_key: int | None = None,
        driver_number: int | None = None,
    ) -> OpenF1Result:
        return await self._get(
            "drivers",
            {"session_key": session_key, "driver_number": driver_number},
            cache_ttl_seconds=self.cache_ttls["drivers"],
        )

    async def get_laps(
        self,
        session_key: int,
        driver_number: int | None = None,
        *,
        lap_number: int | None = None,
    ) -> OpenF1Result:
        return await self._get(
            "laps",
            {
                "session_key": session_key,
                "driver_number": driver_number,
                "lap_number": lap_number,
            },
            cache_ttl_seconds=self.cache_ttls["laps"],
        )

    async def get_stints(
        self,
        session_key: int,
        driver_number: int | None = None,
    ) -> OpenF1Result:
        return await self._get(
            "stints",
            {"session_key": session_key, "driver_number": driver_number},
            cache_ttl_seconds=self.cache_ttls["stints"],
        )

    async def get_weather(self, session_key: int) -> OpenF1Result:
        return await self._get(
            "weather",
            {"session_key": session_key},
            cache_ttl_seconds=self.cache_ttls["weather"],
        )

    async def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        *,
        cache_ttl_seconds: int,
        extra: str | None = None,
    ) -> OpenF1Result:
        clean_params = _clean_params(params)
        key = _cache_key(endpoint, clean_params, extra)
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug("OpenF1 cache hit for %s", endpoint)
            return OpenF1Result(
                data=cached,
                cached=True,
                cache_ttl_seconds=cache_ttl_seconds,
                endpoint=endpoint,
            )

        data = await self._fetch_with_retries(endpoint, clean_params, extra)
        self.cache.set(key, data, ttl=cache_ttl_seconds)
        return OpenF1Result(
            data=data,
            cached=False,
            cache_ttl_seconds=cache_ttl_seconds,
            endpoint=endpoint,
        )

    async def _fetch_with_retries(
        self, endpoint: str, params: dict[str, Any], extra: str | None
    ) -> list[dict[str, Any]]:
        attempt = 0
        while True:
            try:
                await self.rate_limiter.wait()
                path = endpoint if extra is None else f"{endpoint}?{extra}"
                # httpx's `params=` kwarg *replaces* a URL's existing query string,
                # even when it's an empty dict -- which would silently strip the
                # date>/date< filter we just hand-appended into `path` for
                # endpoints like car_data/location (OpenF1's nonstandard operator
                # syntax doesn't fit a normal params dict). Pass None instead of
                # `{}` so httpx leaves the embedded query string alone.
                response = await self._client.get(path, params=params or None)
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response)
                    logger.warning(
                        "OpenF1 rate limited %s request; retry_after=%s",
                        endpoint,
                        retry_after,
                    )
                    raise ProviderRateLimitError(
                        f"OpenF1 rate limit reached for {endpoint}"
                    )
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    await self._sleep_before_retry(endpoint, attempt, response.status_code)
                    attempt += 1
                    continue

                response.raise_for_status()
                return self._parse_response(endpoint, response)

            except ProviderRateLimitError:
                raise
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    await self._sleep_before_retry(endpoint, attempt, "timeout")
                    attempt += 1
                    continue
                logger.warning("OpenF1 timeout for %s after %s attempts", endpoint, attempt + 1)
                raise ProviderTimeoutError(f"OpenF1 timed out while requesting {endpoint}") from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code in RETRYABLE_STATUS_CODES:
                    logger.warning("OpenF1 unavailable for %s: HTTP %s", endpoint, status_code)
                    raise ProviderUnavailableError(
                        f"OpenF1 is unavailable for {endpoint}"
                    ) from exc
                logger.warning("OpenF1 bad status for %s: HTTP %s", endpoint, status_code)
                raise ProviderBadResponseError(
                    f"OpenF1 returned HTTP {status_code} for {endpoint}"
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    await self._sleep_before_retry(endpoint, attempt, "network_error")
                    attempt += 1
                    continue
                logger.warning("OpenF1 network error for %s: %s", endpoint, exc)
                raise ProviderUnavailableError(
                    f"OpenF1 is unavailable for {endpoint}"
                ) from exc

    async def _sleep_before_retry(
        self, endpoint: str, attempt: int, reason: int | str
    ) -> None:
        delay_s = min(2.0, 0.25 * (2 ** attempt))
        logger.info(
            "Retrying OpenF1 %s request after %ss due to %s",
            endpoint,
            delay_s,
            reason,
        )
        await asyncio.sleep(delay_s)

    def _parse_response(self, endpoint: str, response: httpx.Response) -> list[dict[str, Any]]:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("OpenF1 returned invalid JSON for %s", endpoint)
            raise ProviderBadResponseError(
                f"OpenF1 returned invalid JSON for {endpoint}"
            ) from exc

        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            logger.warning(
                "OpenF1 returned malformed payload for %s: %s",
                endpoint,
                type(payload).__name__,
            )
            raise ProviderBadResponseError(
                f"OpenF1 returned malformed data for {endpoint}"
            )
        return payload

    # Backward-compatible list-returning methods used by the analysis services.
    async def sessions(
        self,
        year: int | None = None,
        country_name: str | None = None,
        session_name: str | None = None,
        session_key: int | str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.get_sessions(
            year=year,
            country_name=country_name,
            session_name=session_name,
            session_key=session_key,
        )
        return result.data

    async def drivers(
        self,
        session_key: int | str,
        driver_number: int | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.get_drivers(
            session_key=int(session_key), driver_number=driver_number
        )
        return result.data

    async def laps(
        self,
        session_key: int | str,
        driver_number: int | None = None,
        lap_number: int | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.get_laps(
            session_key=int(session_key),
            driver_number=driver_number,
            lap_number=lap_number,
        )
        return result.data

    async def car_data(
        self,
        session_key: int | str,
        driver_number: int,
        date_gt: str | None = None,
        date_lt: str | None = None,
    ) -> list[dict[str, Any]]:
        parts = [f"session_key={session_key}", f"driver_number={driver_number}"]
        if date_gt:
            parts.append(f"date>{date_gt}")
        if date_lt:
            parts.append(f"date<{date_lt}")
        result = await self._get(
            "car_data",
            cache_ttl_seconds=self.cache_ttls["laps"],
            extra="&".join(parts),
        )
        return result.data

    async def location(
        self,
        session_key: int | str,
        driver_number: int,
        date_gt: str | None = None,
        date_lt: str | None = None,
    ) -> list[dict[str, Any]]:
        parts = [f"session_key={session_key}", f"driver_number={driver_number}"]
        if date_gt:
            parts.append(f"date>{date_gt}")
        if date_lt:
            parts.append(f"date<{date_lt}")
        result = await self._get(
            "location",
            cache_ttl_seconds=self.cache_ttls["laps"],
            extra="&".join(parts),
        )
        return result.data

    async def stints(
        self,
        session_key: int | str,
        driver_number: int | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.get_stints(
            session_key=int(session_key), driver_number=driver_number
        )
        return result.data

    async def pit(
        self,
        session_key: int | str,
        driver_number: int | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._get(
            "pit",
            {"session_key": session_key, "driver_number": driver_number},
            cache_ttl_seconds=self.cache_ttls["stints"],
        )
        return result.data

    async def weather(self, session_key: int | str) -> list[dict[str, Any]]:
        result = await self.get_weather(session_key=int(session_key))
        return result.data


_singleton: OpenF1Client | None = None


def get_client() -> OpenF1Client:
    global _singleton
    if _singleton is None:
        _singleton = OpenF1Client()
    return _singleton
