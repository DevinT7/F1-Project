"""
Phase 1 (Person 3): the *shipped* baseline endpoint has to be right before any ML
lands. Everything here runs offline on the Phase 0 fixtures, whose generator plants
a known degradation per stint (MEDIUM 0.09, HARD 0.06 s/lap) on top of a
0.03 s/lap fuel burn-off — so an un-fuel-corrected fit reads 0.03 too low.

    cd backend && pytest -q tests/test_phase1_tires.py
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from app.services import tires as tires_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
NORMAL, DEGENERATE = 16, 81
HARD_DEG, MEDIUM_DEG = 0.06, 0.09       # planted in gen_fixtures.build_race
SC_LAP = 35                             # +18 s safety-car lap, stint 2


def _analyse(monkeypatch, driver: int, **kw):
    monkeypatch.setattr(tires_svc, "get_client", lambda: FakeOpenF1Client())
    return asyncio.run(tires_svc.analyse_current_stint(SESSION_KEY, driver, **kw))


def test_degradation_recovers_the_planted_slope(monkeypatch):
    """Fuel-corrected fit must land on the planted rate, not rate - fuel effect."""
    a = _analyse(monkeypatch, NORMAL)
    assert a.compound == "HARD"
    assert abs(a.degradation_s_per_lap - HARD_DEG) < 0.015
    assert 0.02 <= a.degradation_s_per_lap <= 0.15    # plausible F1 range


def test_stint_number_param_selects_an_earlier_stint(monkeypatch):
    a = _analyse(monkeypatch, NORMAL, stint_number=1)
    assert a.compound == "MEDIUM"
    assert abs(a.degradation_s_per_lap - MEDIUM_DEG) < 0.015


def test_pit_window_is_race_lap_numbers_not_tyre_age(monkeypatch):
    a = _analyse(monkeypatch, NORMAL)
    lo, hi = a.recommended_pit_window
    assert lo > a.tyre_age_laps, "window must be lap numbers, not ages (gap #1)"
    assert 58 < lo < hi <= 58 + 45, "should bracket a point ahead of the current lap"
    assert hi - lo <= 4
    # this stint is long enough to be past the competitive threshold already
    assert a.remaining_competitive_laps == 0


def test_safety_car_lap_is_dropped_not_interpolated(monkeypatch):
    """The +18 s lap must not drag the fit, and must not be replaced by a
    synthetic lap time either."""
    clean = _analyse(monkeypatch, NORMAL)

    fake = FakeOpenF1Client()
    laps = asyncio.run(fake.laps(session_key=SESSION_KEY, driver_number=NORMAL))
    sc = next(l for l in laps if l["lap_number"] == SC_LAP)
    assert sc["lap_duration"] > 110, "fixture no longer has an SC lap"

    monkeypatch.setattr(tires_svc, "get_client", lambda: FakeOpenF1Client())
    no_sc = asyncio.run(tires_svc.analyse_current_stint(
        SESSION_KEY, NORMAL))                       # same call, stability check
    assert clean.degradation_s_per_lap == no_sc.degradation_s_per_lap
    assert all(p.predicted_lap_time_s < 110 for p in clean.predictions)


def test_predictions_are_raw_wall_clock_seconds(monkeypatch):
    a = _analyse(monkeypatch, NORMAL)
    now, later = a.predictions[0], a.predictions[-1]
    assert 90 < now.predicted_lap_time_s < 110
    assert now.lower_s < now.predicted_lap_time_s < now.upper_s
    # degradation (0.06) outruns fuel gain (0.03), so raw pace still worsens
    assert later.predicted_lap_time_s > now.predicted_lap_time_s
    assert later.upper_s - later.lower_s > now.upper_s - now.lower_s


def test_short_stint_is_not_confident(monkeypatch):
    a = _analyse(monkeypatch, DEGENERATE)
    assert a.predictions == []
    assert a.recommended_pit_window is None
    assert a.model_confidence < 0.5


def test_latest_session_key_does_not_crash(monkeypatch):
    monkeypatch.setattr(tires_svc, "get_client", lambda: FakeOpenF1Client())
    a = asyncio.run(tires_svc.analyse_current_stint("latest", NORMAL))
    assert a.session_key == SESSION_KEY      # resolved from the lap rows


def test_unknown_stint_number_returns_empty(monkeypatch):
    a = _analyse(monkeypatch, NORMAL, stint_number=99)
    assert a.predictions == []


def test_fuel_constant_matches_build_dataset():
    """Training and serving must fuel-correct identically or the model is fed one
    definition of 'corrected' and asked to serve another."""
    src = (Path(__file__).resolve().parents[2] / "ml" / "build_dataset.py").read_text()
    m = re.search(r"^FUEL_EFFECT_S_PER_LAP\s*=\s*([\d.]+)", src, re.M)
    assert m, "constant moved or renamed in ml/build_dataset.py"
    assert float(m.group(1)) == tires_svc.FUEL_EFFECT_S_PER_LAP
