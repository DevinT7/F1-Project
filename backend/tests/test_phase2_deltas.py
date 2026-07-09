"""
Phase 2 (Person 2): time-delta / sector-delta hardening, all offline.

Covers the three Phase 2 definition-of-done items from P2_ANALYSIS_PLAN.md:
  - time_gained_s + time_lost_s reconciles with the endpoints of the delta trace
    (i.e. with total_delta_s, since the trace implicitly starts level at 0).
  - sector_deltas falls back to a delta-trace-derived approximation when the
    `laps` rows don't carry real duration_sector_* fields.
  - /api/analysis/compare wires real lap rows through end-to-end, so
    sector_deltas actually populates via the live router path (not just when
    calling compare_traces directly, as test_offline_compare.py already covers).

    cd backend && pytest -q tests/test_phase2_deltas.py
"""
from __future__ import annotations

import asyncio

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import Driver, TelemetrySample, TelemetryTrace
from app.services import analysis as analysis_svc
from app.services import sessions as sessions_svc
from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
DRIVER, REFERENCE = 55, 4  # Sainz vs Norris, 2023 Singapore GP fixture


def _fake_trace(offset_speed: float, driver_number: int) -> TelemetryTrace:
    """Same synthetic-lap shape as test_smoke.py's helper: a few clean braking
    zones so corner/delta math has something non-trivial to chew on."""
    d = np.arange(0, 4000, 10.0)
    speed = np.full_like(d, 320.0)
    for apex in (800, 1600, 2400, 3200):
        speed -= 190 * np.exp(-((d - apex) ** 2) / (2 * 90.0 ** 2))
    speed = np.clip(speed + offset_speed, 90, 340)
    samples = [TelemetrySample(distance=float(x), speed=float(s)) for x, s in zip(d, speed)]
    return TelemetryTrace(session_key=SESSION_KEY, driver_number=driver_number,
                          lap_number=1, lap_time=92.0, samples=samples)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- gained/lost reconciliation

def test_gained_lost_reconciles_with_total_delta_synthetic():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=44), a, b, include_traces=False,
    )
    assert cmp.time_gained_s <= 0.0
    assert cmp.time_lost_s >= 0.0
    assert cmp.time_gained_s + cmp.time_lost_s == round(
        cmp.time_gained_s + cmp.time_lost_s, 3)  # sanity: still a clean float
    assert abs((cmp.time_gained_s + cmp.time_lost_s) - cmp.total_delta_s) < 1e-6


def test_gained_lost_reconciles_with_total_delta_on_fixtures(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)

    async def _run_traces():
        return (await telemetry_svc.build_trace(SESSION_KEY, DRIVER),
                await telemetry_svc.build_trace(SESSION_KEY, REFERENCE))

    driver_trace, ref_trace = _run(_run_traces())
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=DRIVER), Driver(driver_number=REFERENCE),
        driver_trace, ref_trace, include_traces=False,
    )
    # gained/lost/total are each independently rounded to 3dp for the API contract,
    # so the reconciliation tolerance has to absorb that (not the underlying
    # unrounded math, which matches to float precision -- see the synthetic test).
    assert abs((cmp.time_gained_s + cmp.time_lost_s) - cmp.total_delta_s) < 2e-3


# ---------------------------------------------------------------- sector fallback

def test_sector_fallback_used_when_laps_have_no_sector_fields():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=44), a, b,
        driver_lap={"lap_number": 1}, ref_lap={"lap_number": 1},  # no duration_sector_*
        include_traces=False,
    )
    assert len(cmp.sector_deltas) == 3
    # fallback splits the same delta trace, so the 3 sector deltas must sum back
    # to the total (same implicit zero-at-start baseline as time_gained/time_lost),
    # modulo each sector being independently rounded to 3dp for the API contract.
    assert abs(sum(s.delta_s for s in cmp.sector_deltas) - cmp.total_delta_s) < 2e-3


def test_real_sector_fields_still_take_priority_over_fallback():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    driver_lap = {"duration_sector_1": 30.0, "duration_sector_2": 30.0, "duration_sector_3": 30.0}
    ref_lap = {"duration_sector_1": 30.5, "duration_sector_2": 29.5, "duration_sector_3": 30.0}
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=44), a, b,
        driver_lap=driver_lap, ref_lap=ref_lap, include_traces=False,
    )
    assert [s.delta_s for s in cmp.sector_deltas] == [-0.5, 0.5, 0.0]


def test_sector_deltas_from_laps_empty_when_one_side_missing_falls_back():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    driver_lap = {"duration_sector_1": 30.0}  # ref_lap missing entirely
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=44), a, b,
        driver_lap=driver_lap, ref_lap=None, include_traces=False,
    )
    assert len(cmp.sector_deltas) == 3  # fell back, didn't just return []


# ---------------------------------------------------------------- router wiring (end-to-end)

def test_router_populates_sector_deltas_end_to_end(monkeypatch):
    """Before Phase 2, /api/analysis/compare never fetched lap rows, so
    sector_deltas always came back empty even though the fixture laps carry real
    duration_sector_* fields. This proves the router now wires them through."""
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY,
        "driver_number": DRIVER,
        "reference_driver_number": REFERENCE,
        "include_traces": False,
    })
    assert r.status_code == 200
    body = r.json()

    assert body["sector_deltas"], "router should now populate sector deltas from real lap rows"
    by_sector = {s["sector"]: s["delta_s"] for s in body["sector_deltas"]}
    # from the fixture laps.json: driver (55) vs reference (4) duration_sector_*
    assert by_sector[1] == round(29.592 - 29.455, 3)
    assert by_sector[2] == round(30.561 - 30.462, 3)
    assert by_sector[3] == round(29.232 - 29.526, 3)

    assert body["delta_trace"], "delta trace should not be empty"
    assert abs((body["time_gained_s"] + body["time_lost_s"]) - body["total_delta_s"]) < 2e-3


def test_router_falls_back_to_derived_sectors_when_lap_row_missing(monkeypatch):
    """If get_lap_row can't find a matching row (e.g. lap not in the laps list for
    some reason), the router must not error -- compare_traces should fall back to
    the delta-trace-derived sector approximation instead of returning empty."""
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    async def _no_lap_row(session_key, driver_number, lap_number):
        return None
    monkeypatch.setattr(telemetry_svc, "get_lap_row", _no_lap_row)

    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY,
        "driver_number": DRIVER,
        "reference_driver_number": REFERENCE,
        "include_traces": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["sector_deltas"]) == 3, "should fall back to derived sectors, not go empty"
