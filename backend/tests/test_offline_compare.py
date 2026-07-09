"""
Phase 0 offline harness (Person 2): run the *full* compare path — build_trace x2,
driver metadata lookup, compare_traces — against saved fixtures with the real
network client never touched. This is the proof that P2 doesn't have to wait on
P1's live telemetry endpoint to build/verify the analysis engine.

    cd backend && pytest -q tests/test_offline_compare.py
"""
from __future__ import annotations

import asyncio

from app.models.schemas import LapComparison
from app.services import analysis as analysis_svc
from app.services import sessions as sessions_svc
from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
DRIVER, REFERENCE = 55, 4  # Sainz vs Norris, 2023 Singapore GP


def test_full_compare_path_runs_offline(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    async def _run():
        driver_trace = await telemetry_svc.build_trace(SESSION_KEY, DRIVER)
        ref_trace = await telemetry_svc.build_trace(SESSION_KEY, REFERENCE)
        driver_meta = await sessions_svc.get_driver(SESSION_KEY, DRIVER)
        ref_meta = await sessions_svc.get_driver(SESSION_KEY, REFERENCE)
        laps = await fake.laps(session_key=SESSION_KEY)
        return driver_trace, ref_trace, driver_meta, ref_meta, laps

    driver_trace, ref_trace, driver_meta, ref_meta, laps = asyncio.run(_run())

    assert driver_trace.samples, "fixture should produce a non-empty driver trace"
    assert ref_trace.samples, "fixture should produce a non-empty reference trace"
    assert driver_meta.name_acronym == "SAI"
    assert ref_meta.name_acronym == "NOR"

    driver_lap = next(l for l in laps if l["driver_number"] == DRIVER)
    ref_lap = next(l for l in laps if l["driver_number"] == REFERENCE)

    cmp: LapComparison = analysis_svc.compare_traces(
        driver_meta, ref_meta, driver_trace, ref_trace,
        driver_lap=driver_lap, ref_lap=ref_lap, include_traces=False,
    )

    # contract shape sanity (Phase 0's other goal: shapes agreed & frozen)
    assert cmp.session_key == SESSION_KEY
    assert cmp.delta_trace, "delta trace should not be empty"
    assert cmp.sector_deltas, "real lap rows were passed, sector deltas must populate"
    assert len(cmp.corners) >= 5, "Marina-Bay-style fixture should yield several corners"
    assert isinstance(cmp.total_delta_s, float)
