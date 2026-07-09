"""
Phase 4 (Person 2): structured insight assembly, all offline.

Covers the Phase 4 definition-of-done items from P2_ANALYSIS_PLAN.md:
  - CornerInsight carries the vs-reference deltas (braking_delta_m,
    min_speed_delta_kmh, throttle_delay_s) the project doc's example finding
    needs, with the documented sign convention, alongside the driver's own
    absolute values P4 charts.
  - ai_engineer reproduces that example finding style ("lost 0.21s in Turn 7,
    braked 18m earlier, full throttle 0.4s later") purely by rephrasing
    numbers P2 already computed -- no new math, no invented stats.
  - include_traces cleanly includes/omits the heavy per-sample payload via the
    live router, so P3 (light) and P4 (heavy) each get exactly what they ask for.

    cd backend && pytest -q tests/test_phase4_insights.py
"""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import (
    AIReportRequest, CornerInsight, Driver, TelemetrySample, TelemetryTrace,
)
from app.services import analysis as analysis_svc
from app.services import ai_engineer
from app.services import sessions as sessions_svc
from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
DRIVER, REFERENCE = 55, 4


def _corner_trace(driver_number: int, apex_d: float = 500.0, sigma_in: float = 60.0,
                  sigma_out: float = 60.0, depth: float = 150.0, v0: float = 300.0,
                  length: float = 1000.0, step: float = 2.0) -> TelemetryTrace:
    """One controlled braking zone (Gaussian entry/exit, independently
    parameterised) so braking-point and min-speed can be tuned in isolation."""
    d = np.arange(0, length, step)
    speed = np.where(
        d <= apex_d,
        v0 - depth * np.exp(-((d - apex_d) ** 2) / (2 * sigma_in ** 2)),
        v0 - depth * np.exp(-((d - apex_d) ** 2) / (2 * sigma_out ** 2)),
    )
    throttle = np.clip((speed - 90) / 2.0, 0, 100)
    samples = [TelemetrySample(distance=float(x), speed=float(s), throttle=float(t))
              for x, s, t in zip(d, speed, throttle)]
    return TelemetryTrace(session_key=SESSION_KEY, driver_number=driver_number,
                          lap_number=1, lap_time=90.0, samples=samples)


# ---------------------------------------------------------------- braking_delta_m sign

def test_braking_delta_m_positive_when_driver_brakes_later():
    # smaller sigma_in => steeper/later braking (inflection closer to apex)
    driver = _corner_trace(1, sigma_in=40.0)
    reference = _corner_trace(44, sigma_in=90.0)
    known = [{"corner_number": 1, "distance": 500.0}]
    corners = analysis_svc.detect_corners(driver, reference, known=known)
    assert len(corners) == 1
    c = corners[0]
    assert c.braking_delta_m is not None
    assert c.braking_delta_m > 10.0, (
        "driver's steeper/later braking profile should register a meaningfully "
        "larger (later) braking distance than the reference's gradual one"
    )
    # isolated by construction: same apex + depth => min speed shouldn't differ much
    assert abs(c.min_speed_delta_kmh) < 5.0


def test_braking_delta_m_negative_when_driver_brakes_earlier():
    driver = _corner_trace(1, sigma_in=90.0)
    reference = _corner_trace(44, sigma_in=40.0)
    known = [{"corner_number": 1, "distance": 500.0}]
    corners = analysis_svc.detect_corners(driver, reference, known=known)
    assert corners[0].braking_delta_m < -10.0


# ---------------------------------------------------------------- min_speed_delta_kmh sign

def test_min_speed_delta_kmh_positive_when_driver_carries_more_speed():
    driver = _corner_trace(1, depth=120.0)       # shallower dip -> higher min speed
    reference = _corner_trace(44, depth=180.0)   # deeper dip -> lower min speed
    known = [{"corner_number": 1, "distance": 500.0}]
    corners = analysis_svc.detect_corners(driver, reference, known=known)
    c = corners[0]
    assert c.min_speed_delta_kmh > 30.0
    # isolated by construction: same sigma both sides => braking point shouldn't move much
    assert abs(c.braking_delta_m) < 5.0


def test_min_speed_delta_kmh_negative_when_driver_carries_less_speed():
    driver = _corner_trace(1, depth=180.0)
    reference = _corner_trace(44, depth=120.0)
    known = [{"corner_number": 1, "distance": 500.0}]
    corners = analysis_svc.detect_corners(driver, reference, known=known)
    assert corners[0].min_speed_delta_kmh < -30.0


# ---------------------------------------------------------------- ai_engineer narrative

def test_corner_finding_phrase_reproduces_project_doc_example():
    corner = CornerInsight(
        corner_number=7, corner_name=None, distance=1234.0,
        entry_speed=210.0, min_speed=140.0, exit_speed=230.0,
        braking_point_m=1200.0, braking_delta_m=-18.0,
        throttle_delay_s=0.4, min_speed_delta_kmh=-5.0,
        time_delta_s=0.21,
    )
    phrase = ai_engineer._corner_finding_phrase(ai_engineer._corner_fact(corner))
    assert phrase == "lost 0.21s in Turn 7, braked 18m earlier, carried 5 km/h less min speed, full throttle 0.40s later"


def test_biggest_loss_recommendation_uses_real_computed_numbers_only():
    """The AI report's recommendation text must be built entirely from the
    CornerInsight's own fields -- never a re-derivation or an invented number."""
    corner = CornerInsight(
        corner_number=7, corner_name="Turn 7", distance=1234.0,
        entry_speed=210.0, min_speed=140.0, exit_speed=230.0,
        braking_point_m=1200.0, braking_delta_m=-18.0,
        throttle_delay_s=0.4, min_speed_delta_kmh=None,
        time_delta_s=0.21,
    )
    cmp = analysis_svc.compare_traces(
        Driver(driver_number=1), Driver(driver_number=44),
        _corner_trace(1), _corner_trace(44), include_traces=False,
    )
    cmp.corners = [corner]  # substitute a controlled corner for a deterministic assertion
    report = ai_engineer.build_report(AIReportRequest(comparison=cmp))
    assert report.grounded is True
    joined = " ".join(report.recommendations)
    assert "Turn 7" in joined
    assert "0.21s" in joined
    assert "18m earlier" in joined
    assert "0.40s later" in joined


# ---------------------------------------------------------------- include_traces toggle (router)

def test_include_traces_true_returns_full_samples(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY, "driver_number": DRIVER,
        "reference_driver_number": REFERENCE, "include_traces": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["driver_trace"] is not None
    assert body["reference_trace"] is not None
    assert len(body["driver_trace"]["samples"]) > 100
    assert len(body["reference_trace"]["samples"]) > 100


def test_include_traces_false_omits_samples(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)
    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY, "driver_number": DRIVER,
        "reference_driver_number": REFERENCE, "include_traces": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["driver_trace"] is None
    assert body["reference_trace"] is None
    # the light payload still has everything P3/the AI engineer need
    assert body["delta_trace"]
    assert body["corners"]
    assert body["sector_deltas"]
