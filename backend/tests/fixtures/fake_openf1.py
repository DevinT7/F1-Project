"""
Fake OpenF1 client — drop-in replacement for `app.clients.openf1.OpenF1Client`
that reads the saved JSON in this directory instead of hitting the network.

Mirrors the real client's async method signatures and filtering semantics
(`session_key`, `driver_number`, `date_gt`/`date_lt` on `car_data`) so
`telemetry.py` / `analysis.py` / `sessions.py` can run unmodified against it.

Usage in a test:

    from tests.fixtures.fake_openf1 import FakeOpenF1Client
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def _load(name: str) -> list[dict]:
    path = HERE / name
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


class FakeOpenF1Client:
    """No network, no caching, no rate limits — just filters saved fixtures."""

    def __init__(self) -> None:
        self._sessions = _load("sessions.json")
        self._drivers = _load("drivers.json")
        self._laps = _load("laps.json")
        self._car_data = {
            55: _load("car_data_55.json"),
            4: _load("car_data_4.json"),
        }

    async def aclose(self) -> None:
        pass

    async def sessions(self, year: int | None = None, country_name: str | None = None,
                       session_name: str | None = None,
                       session_key: int | str | None = None) -> list[dict]:
        rows = self._sessions
        if session_key is not None:
            rows = [r for r in rows if r["session_key"] == int(session_key)]
        if year is not None:
            rows = [r for r in rows if r.get("year") == year]
        if country_name is not None:
            rows = [r for r in rows if r.get("country_name") == country_name]
        if session_name is not None:
            rows = [r for r in rows if r.get("session_name") == session_name]
        return rows

    async def drivers(self, session_key: int | str,
                      driver_number: int | None = None) -> list[dict]:
        rows = [r for r in self._drivers if r["session_key"] == int(session_key)]
        if driver_number is not None:
            rows = [r for r in rows if r["driver_number"] == driver_number]
        return rows

    async def laps(self, session_key: int | str, driver_number: int | None = None,
                   lap_number: int | None = None) -> list[dict]:
        rows = [r for r in self._laps if r["session_key"] == int(session_key)]
        if driver_number is not None:
            rows = [r for r in rows if r["driver_number"] == driver_number]
        if lap_number is not None:
            rows = [r for r in rows if r["lap_number"] == lap_number]
        return rows

    async def car_data(self, session_key: int | str, driver_number: int,
                       date_gt: str | None = None, date_lt: str | None = None) -> list[dict]:
        rows = self._car_data.get(driver_number, [])
        rows = [r for r in rows if r["session_key"] == int(session_key)]
        if date_gt is not None:
            gt = _parse(date_gt)
            rows = [r for r in rows if _parse(r["date"]) > gt]
        if date_lt is not None:
            lt = _parse(date_lt)
            rows = [r for r in rows if _parse(r["date"]) < lt]
        return rows

    # Not used by P2's Phase 0 path, but present so the fake is a full drop-in.
    async def location(self, session_key: int | str, driver_number: int,
                       date_gt: str | None = None, date_lt: str | None = None) -> list[dict]:
        return []

    async def stints(self, session_key: int | str,
                     driver_number: int | None = None) -> list[dict]:
        return []

    async def pit(self, session_key: int | str,
                  driver_number: int | None = None) -> list[dict]:
        return []

    async def weather(self, session_key: int | str) -> list[dict]:
        return []
