"""
Phase 5 (Person 2): testing, validation & hardening, all offline.

Covers the Phase 5 definition-of-done items from P2_ANALYSIS_PLAN.md:
  - Traffic/safety-car/red-flag-affected laps are flagged (not silently treated
    as routine) via TelemetryTrace.is_anomalous_lap -> LapComparison.warnings.
  - The router handles bad input cleanly: 400 for a meaningless self-comparison,
    404 when a lap has no usable telemetry, 502 on an OpenF1 upstream failure --
    none of them raise an unhandled exception.
  - Repeat calls for identical params are served from cache.py, not the network
    (confirms P1's cache does its job for P2's call pattern).

    cd backend && pytest -q tests/test_phase5_hardening.py
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app import cache as cache_mod
from app.clients.openf1 import OpenF1Client, OpenF1Error
from app.main import app
from app.services import analysis as analysis_svc
from app.services import sessions as sessions_svc
from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
DRIVER, REFERENCE = 55, 4


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- anomaly detection

def test_clean_lap_not_flagged():
    others = [88.9, 89.1, 89.3, 89.0, 88.8]
    assert telemetry_svc._is_anomalous_lap_duration(89.2, others) is False


def test_safety_car_affected_lap_is_flagged():
    others = [88.9, 89.1, 89.3, 89.0, 88.8]
    assert telemetry_svc._is_anomalous_lap_duration(115.0, others) is True


def test_single_lap_never_flagged_no_baseline_to_compare():
    assert telemetry_svc._is_anomalous_lap_duration(200.0, []) is False


def test_uniformly_slow_session_not_flagged_ratio_guard():
    # every lap is ~1s apart -- MAD alone would flag the slowest as an outlier;
    # the ratio guard should still hold since it's nowhere near "much slower".
    others = [90.0, 91.0, 92.0, 93.0]
    assert telemetry_svc._is_anomalous_lap_duration(94.0, others) is False


class _LapsClient:
    """Minimal fake client: several clean laps + one clearly SC-affected lap."""

    def __init__(self):
        self._laps = [
            {"session_key": SESSION_KEY, "driver_number": 1, "lap_number": n,
             "date_start": f"2023-09-17T12:{n:02d}:00Z", "lap_duration": 89.0 + n * 0.1,
             "is_pit_out_lap": False}
            for n in range(1, 6)
        ] + [
            {"session_key": SESSION_KEY, "driver_number": 1, "lap_number": 6,
             "date_start": "2023-09-17T12:06:00Z", "lap_duration": 140.0,  # SC/VSC-style lap
             "is_pit_out_lap": False},
        ]
        self._car_data = [
            {"date": "2023-09-17T12:00:00.0Z", "speed": 200},
            {"date": "2023-09-17T12:00:01.0Z", "speed": 210},
            {"date": "2023-09-17T12:00:02.0Z", "speed": 220},
        ]

    async def laps(self, session_key, driver_number=None, lap_number=None):
        rows = list(self._laps)
        if lap_number is not None:
            rows = [r for r in rows if r["lap_number"] == lap_number]
        return rows

    async def car_data(self, session_key, driver_number, date_gt=None, date_lt=None):
        return list(self._car_data)


def test_build_trace_flags_explicit_anomalous_lap(monkeypatch):
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: _LapsClient())
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, lap_number=6))
    assert trace.samples, "still returns the requested lap -- flagging, not skipping"
    assert trace.is_anomalous_lap is True


def test_build_trace_default_fastest_lap_not_flagged(monkeypatch):
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: _LapsClient())
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1))  # no lap_number -> fastest
    assert trace.samples
    assert trace.lap_number != 6, "the SC-affected lap should never be the 'fastest' pick"
    assert trace.is_anomalous_lap is False


def test_compare_traces_surfaces_anomaly_warning(monkeypatch):
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: _LapsClient())
    anomalous = _run(telemetry_svc.build_trace(SESSION_KEY, 1, lap_number=6))
    clean = _run(telemetry_svc.build_trace(SESSION_KEY, 1, lap_number=1))

    from app.models.schemas import Driver
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1, name_acronym="XXX"), Driver(driver_number=1, name_acronym="XXX"),
        anomalous, clean, include_traces=False,
    )
    assert cmp.warnings, "an is_anomalous_lap trace must produce an explicit caveat"
    assert "XXX" in cmp.warnings[0]
    assert "lap 6" in cmp.warnings[0]


def test_compare_traces_no_warnings_when_both_laps_clean(monkeypatch):
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: _LapsClient())
    a = _run(telemetry_svc.build_trace(SESSION_KEY, 1, lap_number=1))
    b = _run(telemetry_svc.build_trace(SESSION_KEY, 1, lap_number=2))

    from app.models.schemas import Driver
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=1), a, b, include_traces=False,
    )
    assert cmp.warnings == []


# ---------------------------------------------------------------- router bad-input handling

def test_router_rejects_identical_driver_and_lap_selection():
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY, "driver_number": DRIVER, "reference_driver_number": DRIVER,
    })
    assert r.status_code == 400


def test_router_allows_same_driver_different_laps(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY, "driver_number": DRIVER, "reference_driver_number": DRIVER,
        "lap_number": 18, "reference_lap_number": 17,  # different laps, same driver: allowed
        "include_traces": False,
    })
    # fixture only has lap 18 for driver 55; lap 17 has no telemetry -> 404, not 400.
    # The point of this test is that it's NOT rejected as a same-lap comparison.
    assert r.status_code != 400


class _EmptyLapsClient:
    async def laps(self, session_key, driver_number=None, lap_number=None):
        return []

    async def car_data(self, session_key, driver_number, date_gt=None, date_lt=None):
        return []

    async def drivers(self, session_key, driver_number=None):
        return []


def test_router_404_when_no_telemetry(monkeypatch):
    empty = _EmptyLapsClient()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: empty)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: empty)
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": 0, "driver_number": 1, "reference_driver_number": 2,
    })
    assert r.status_code == 404


class _BoomClient:
    async def laps(self, session_key, driver_number=None, lap_number=None):
        raise OpenF1Error("OpenF1 laps -> 503")


def test_router_502_on_openf1_upstream_failure(monkeypatch):
    boom = _BoomClient()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: boom)
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY, "driver_number": 1, "reference_driver_number": 2,
    })
    assert r.status_code == 502


# ---------------------------------------------------------------- caching (confirm P1's cache.py)

def _no_proxy_env(monkeypatch):
    # This sandbox routes all egress through a SOCKS proxy env var for network
    # isolation; httpx's default trust_env=True would try to honor it (and fail
    # importing optional socks support) purely to *construct* the client, before
    # our mocked .get() ever runs. Strip it for these two tests -- they never
    # touch the network either way, real proxy or not.
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
               "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)


def test_repeat_openf1_calls_are_served_from_cache_not_network(monkeypatch):
    """No real network is ever touched -- only the underlying httpx.AsyncClient.get
    is mocked -- so this proves cache.py (not luck) is what avoids the second
    request, which is exactly what keeps repeat /api/analysis/compare calls cheap
    and within OpenF1's free-tier rate limit."""
    _no_proxy_env(monkeypatch)
    cache_mod.clear()
    client = OpenF1Client(base_url="https://example.invalid/v1")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=[
        {"session_key": SESSION_KEY, "driver_number": 55, "lap_number": 18, "lap_duration": 89.385},
    ])
    client._client.get = AsyncMock(return_value=fake_resp)

    async def _run_calls():
        first = await client.laps(session_key=SESSION_KEY, driver_number=55)
        second = await client.laps(session_key=SESSION_KEY, driver_number=55)
        return first, second

    first, second = _run(_run_calls())
    assert first == second
    assert client._client.get.call_count == 1, (
        "identical params should be served from cache.py on the second call")


def test_different_params_are_not_conflated_by_the_cache(monkeypatch):
    _no_proxy_env(monkeypatch)
    cache_mod.clear()
    client = OpenF1Client(base_url="https://example.invalid/v1")

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=[{"session_key": SESSION_KEY, "driver_number": 4}])
    client._client.get = AsyncMock(return_value=fake_resp)

    async def _run_calls():
        await client.laps(session_key=SESSION_KEY, driver_number=55)
        await client.laps(session_key=SESSION_KEY, driver_number=4)  # different driver

    _run(_run_calls())
    assert client._client.get.call_count == 2, "different params must not share a cache entry"
