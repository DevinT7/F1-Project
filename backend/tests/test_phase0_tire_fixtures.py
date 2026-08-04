"""
Phase 0 offline harness (Person 3): `analyse_current_stint` must run end-to-end
against saved fixtures with the network off, and must not fall into `_empty` on
the normal-stint case.

    cd backend && pytest -q tests/test_phase0_tire_fixtures.py
"""
from __future__ import annotations

import asyncio

from app.services import tires as tires_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
NORMAL, DEGENERATE = 16, 81


def _analyse(monkeypatch, driver: int):
    monkeypatch.setattr(tires_svc, "get_client", lambda: FakeOpenF1Client())
    return asyncio.run(tires_svc.analyse_current_stint(SESSION_KEY, driver))


def test_normal_stint_is_populated_offline(monkeypatch):
    a = _analyse(monkeypatch, NORMAL)
    assert a.compound == "HARD"           # last stint of the 2-stint fixture race
    assert a.predictions, "should not be the _empty path"
    assert a.degradation_s_per_lap > 0
    assert a.tyre_age_laps and a.tyre_age_laps > 10


def test_degenerate_stint_returns_empty(monkeypatch):
    a = _analyse(monkeypatch, DEGENERATE)
    assert a.predictions == []
    assert a.recommended_pit_window is None


def test_fixture_edge_cases_present():
    fake = FakeOpenF1Client()
    laps = asyncio.run(fake.laps(session_key=SESSION_KEY, driver_number=NORMAL))
    assert any(l["is_pit_out_lap"] for l in laps), "pit-out lap"
    assert any(l["lap_duration"] is None for l in laps), "missing lap_duration"
    assert max(l["lap_duration"] or 0 for l in laps) > 110, "safety-car lap"
    assert asyncio.run(fake.pit(session_key=SESSION_KEY, driver_number=NORMAL))
    assert asyncio.run(fake.weather(session_key=SESSION_KEY))
