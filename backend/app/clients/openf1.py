"""
OpenF1 REST client — https://openf1.org  (Person 1).

Thin async wrapper over the 18 OpenF1 endpoints we use. Everything keys off
`session_key` (+ usually `driver_number`). Responses are cached because historical
data never changes. Numeric operator filters (e.g. speed>=315) are supported by
passing them as raw query strings via `extra`.

Free tier limits: 3 req/s, 30 req/min — so cache and don't hammer it.
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import cache
from ..config import settings


class OpenF1Error(RuntimeError):
    pass


class OpenF1Client:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.openf1_base_url).rstrip("/")
        headers = {}
        if settings.openf1_api_key:  # only needed for the live/sponsor tier
            headers["Authorization"] = f"Bearer {settings.openf1_api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=settings.request_timeout_s
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None,
                   extra: str | None = None) -> list[dict]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"/{endpoint}"
        # operator filters like "speed>=315" can't go through normal params encoding
        if extra:
            url = f"{url}?{extra}"
        cache_key = f"openf1:{url}:{sorted(params.items())}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            raise OpenF1Error(f"OpenF1 {endpoint} -> {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise OpenF1Error(f"OpenF1 {endpoint} request failed: {e}") from e
        if not isinstance(data, list):
            data = [data]
        cache.set(cache_key, data)
        return data

    # ---- endpoints ----
    async def sessions(self, year: int | None = None, country_name: str | None = None,
                       session_name: str | None = None,
                       session_key: int | str | None = None) -> list[dict]:
        return await self._get("sessions", {
            "year": year, "country_name": country_name,
            "session_name": session_name, "session_key": session_key,
        })

    async def drivers(self, session_key: int | str,
                      driver_number: int | None = None) -> list[dict]:
        return await self._get("drivers", {
            "session_key": session_key, "driver_number": driver_number})

    async def laps(self, session_key: int | str, driver_number: int | None = None,
                   lap_number: int | None = None) -> list[dict]:
        return await self._get("laps", {
            "session_key": session_key, "driver_number": driver_number,
            "lap_number": lap_number})

    async def car_data(self, session_key: int | str, driver_number: int,
                       date_gt: str | None = None, date_lt: str | None = None) -> list[dict]:
        # date range narrows a single lap. OpenF1 supports date>... / date<...
        parts = [f"session_key={session_key}", f"driver_number={driver_number}"]
        if date_gt:
            parts.append(f"date>{date_gt}")
        if date_lt:
            parts.append(f"date<{date_lt}")
        return await self._get("car_data", extra="&".join(parts))

    async def location(self, session_key: int | str, driver_number: int,
                       date_gt: str | None = None, date_lt: str | None = None) -> list[dict]:
        parts = [f"session_key={session_key}", f"driver_number={driver_number}"]
        if date_gt:
            parts.append(f"date>{date_gt}")
        if date_lt:
            parts.append(f"date<{date_lt}")
        return await self._get("location", extra="&".join(parts))

    async def stints(self, session_key: int | str,
                     driver_number: int | None = None) -> list[dict]:
        return await self._get("stints", {
            "session_key": session_key, "driver_number": driver_number})

    async def pit(self, session_key: int | str,
                  driver_number: int | None = None) -> list[dict]:
        return await self._get("pit", {
            "session_key": session_key, "driver_number": driver_number})

    async def weather(self, session_key: int | str) -> list[dict]:
        return await self._get("weather", {"session_key": session_key})


_singleton: OpenF1Client | None = None


def get_client() -> OpenF1Client:
    global _singleton
    if _singleton is None:
        _singleton = OpenF1Client()
    return _singleton
