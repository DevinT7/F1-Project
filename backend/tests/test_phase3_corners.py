"""
Phase 3 (Person 2): corner detection engine, all offline.

Covers the Phase 3 definition-of-done items from P2_ANALYSIS_PLAN.md:
  - detect_corners returns one CornerInsight per real corner when a known list is
    supplied (FastF1 circuit info), with sensible entry/min/exit speeds, a braking
    point, throttle delay, and a signed time delta.
  - throttle_delay_s is derived from the corner's own apex speed, not a fixed
    constant, so a slow hairpin and a fast kink convert the same distance gap to
    different times.
  - Corner detection (and the router) degrades gracefully to the speed-minima
    heuristic when FastF1 circuit info is unavailable or errors.

    cd backend && pytest -q tests/test_phase3_corners.py
"""
from __future__ import annotations

import asyncio

import numpy as np
from fastapi.testclient import TestClient

from app.clients import fastf1_client
from app.main import app
from app.models.schemas import Driver, TelemetrySample, TelemetryTrace
from app.services import analysis as analysis_svc
from app.services import sessions as sessions_svc
from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159
DRIVER, REFERENCE = 55, 4


def _fake_trace(offset_speed: float, driver_number: int) -> TelemetryTrace:
    d = np.arange(0, 4000, 10.0)
    speed = np.full_like(d, 320.0)
    for apex in (800, 1600, 2400, 3200):
        speed -= 190 * np.exp(-((d - apex) ** 2) / (2 * 90.0 ** 2))
    speed = np.clip(speed + offset_speed, 90, 340)
    throttle = np.clip((speed - 90) / 2.5, 0, 100)
    samples = [TelemetrySample(distance=float(x), speed=float(s), throttle=float(t))
              for x, s, t in zip(d, speed, throttle)]
    return TelemetryTrace(session_key=SESSION_KEY, driver_number=driver_number,
                          lap_number=1, lap_time=92.0, samples=samples)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- known-corner mode

def test_known_corners_produce_exactly_one_insight_each():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    known = [
        {"corner_number": 1, "corner_name": "Turn 1", "distance": 800.0},
        {"corner_number": 2, "corner_name": "Turn 2", "distance": 1600.0},
        {"corner_number": 3, "corner_name": "Turn 3", "distance": 2400.0},
        {"corner_number": 4, "corner_name": "Turn 4", "distance": 3200.0},
    ]
    corners = analysis_svc.detect_corners(a, b, known=known)
    assert len(corners) == len(known), "one CornerInsight per known corner, no more, no less"
    for insight, k in zip(corners, known):
        assert insight.corner_number == k["corner_number"]
        assert insight.corner_name == k["corner_name"]
        assert abs(insight.distance - k["distance"]) < 20.0, "should snap to the nearest grid point"
        # a real braking zone: min speed should be a genuine dip below both shoulders
        assert insight.min_speed < insight.entry_speed
        assert insight.min_speed < insight.exit_speed
        assert insight.braking_point_m is not None
        assert insight.braking_point_m <= insight.distance
        assert isinstance(insight.time_delta_s, float)


def test_unknown_corner_falls_back_to_speed_minima():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    corners = analysis_svc.detect_corners(a, b, known=None)
    assert len(corners) >= 4, "should still find the four synthetic braking zones"
    assert all(c.corner_name is None for c in corners), "speed-minima mode has no names"


# ---------------------------------------------------------------- throttle delay math

def test_throttle_delay_scales_with_local_apex_speed_not_a_fixed_constant():
    grid = np.arange(0, 100, 10.0)
    thr_a = np.array([0, 0, 0, 0, 0, 100, 100, 100, 100, 100], dtype=float)  # full @ dist=50
    thr_b = np.array([0, 0, 0, 100, 100, 100, 100, 100, 100, 100], dtype=float)  # full @ dist=30

    fast_apex = analysis_svc._throttle_delay(grid, thr_a, thr_b, apex=0, hi=9,
                                             apex_speed_kmh=144.0)  # 40 m/s
    slow_apex = analysis_svc._throttle_delay(grid, thr_a, thr_b, apex=0, hi=9,
                                             apex_speed_kmh=36.0)   # 10 m/s

    assert fast_apex == round(20.0 / 40.0, 3)
    assert slow_apex == round(20.0 / 10.0, 3)
    assert slow_apex > fast_apex, (
        "same 20m distance gap must take longer to convert to a time at a slower "
        "corner exit speed -- a fixed constant would have given identical numbers"
    )


def test_throttle_delay_floors_near_zero_apex_speed():
    grid = np.arange(0, 100, 10.0)
    thr_a = np.array([0, 0, 0, 0, 0, 100, 100, 100, 100, 100], dtype=float)
    thr_b = np.array([0, 0, 0, 100, 100, 100, 100, 100, 100, 100], dtype=float)
    # a near-stationary apex_speed_kmh must not blow up the division to infinity
    delay = analysis_svc._throttle_delay(grid, thr_a, thr_b, apex=0, hi=9, apex_speed_kmh=0.0)
    assert delay == round(20.0 / analysis_svc.MIN_APEX_SPEED_MS, 3)


def test_throttle_delay_none_when_full_throttle_never_reached():
    grid = np.arange(0, 100, 10.0)
    thr_a = np.zeros(10)
    thr_b = np.zeros(10)
    assert analysis_svc._throttle_delay(grid, thr_a, thr_b, apex=0, hi=9, apex_speed_kmh=100.0) is None


# ---------------------------------------------------------------- get_known_corners (FastF1 wiring)

def test_get_known_corners_returns_none_when_meta_missing():
    assert _run(analysis_svc.get_known_corners(None)) is None


def test_get_known_corners_returns_none_on_fastf1_failure(monkeypatch):
    async def _boom(year, gp, session_name):
        raise RuntimeError("fastf1 not installed / session unavailable")
    monkeypatch.setattr(fastf1_client, "circuit_corners", _boom)
    result = _run(analysis_svc.get_known_corners((2023, "Singapore", "Race")))
    assert result is None


def test_get_known_corners_returns_list_on_success(monkeypatch):
    async def _fake(year, gp, session_name):
        return [{"corner_number": 1, "distance": 120.0, "angle": 45.0}]
    monkeypatch.setattr(fastf1_client, "circuit_corners", _fake)
    result = _run(analysis_svc.get_known_corners((2023, "Singapore", "Race")))
    assert result == [{"corner_number": 1, "distance": 120.0, "angle": 45.0}]


# ---------------------------------------------------------------- router wiring (end-to-end)

def test_router_uses_known_corners_when_prefer_fastf1_available(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    async def _fake_corners(year, gp, session_name):
        return [
            {"corner_number": 1, "corner_name": "Turn 1", "distance": 200.0},
            {"corner_number": 2, "corner_name": "Turn 2", "distance": 1500.0},
            {"corner_number": 3, "corner_name": "Turn 3", "distance": 3200.0},
        ]
    monkeypatch.setattr(fastf1_client, "circuit_corners", _fake_corners)

    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY,
        "driver_number": DRIVER,
        "reference_driver_number": REFERENCE,
        "include_traces": False,
        "prefer_fastf1": True,
        "year": 2023,
        "gp": "Singapore",
        "session_name": "Race",
        # no driver_code/reference_driver_code -> telemetry stays on the OpenF1
        # fixture path; only the corner source is FastF1, isolating what we test
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["corners"]) == 3
    assert [c["corner_name"] for c in body["corners"]] == ["Turn 1", "Turn 2", "Turn 3"]


def test_router_falls_back_to_speed_minima_when_circuit_info_errors(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    async def _boom(year, gp, session_name):
        raise RuntimeError("circuit info unavailable")
    monkeypatch.setattr(fastf1_client, "circuit_corners", _boom)

    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY,
        "driver_number": DRIVER,
        "reference_driver_number": REFERENCE,
        "include_traces": False,
        "prefer_fastf1": True,
        "year": 2023,
        "gp": "Singapore",
        "session_name": "Race",
    })
    assert r.status_code == 200, "a broken circuit-info lookup must not break the whole comparison"
    body = r.json()
    assert len(body["corners"]) >= 5, "should degrade gracefully to speed-minima corners"
    assert all(c["corner_name"] is None for c in body["corners"])


def test_router_without_prefer_fastf1_never_calls_circuit_corners(monkeypatch):
    fake = FakeOpenF1Client()
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: fake)
    monkeypatch.setattr(sessions_svc, "get_client", lambda: fake)

    async def _should_not_be_called(year, gp, session_name):
        raise AssertionError("circuit_corners must not be called when prefer_fastf1 is unset")
    monkeypatch.setattr(fastf1_client, "circuit_corners", _should_not_be_called)

    client = TestClient(app)
    r = client.get("/api/analysis/compare", params={
        "session_key": SESSION_KEY,
        "driver_number": DRIVER,
        "reference_driver_number": REFERENCE,
        "include_traces": False,
    })
    assert r.status_code == 200
