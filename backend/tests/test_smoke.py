"""
Smoke tests that run WITHOUT network by mocking the OpenF1 client with fixtures.
This is the pattern each teammate uses to build in isolation.

    cd backend && pytest -q
"""
from __future__ import annotations

import asyncio

import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import (
    AIReportRequest, Driver, LapComparison, TelemetrySample, TelemetryTrace,
)
from app.services import analysis, ai_engineer

client = TestClient(app)


def _fake_trace(offset_speed: float, session_key=9999, driver_number=1) -> TelemetryTrace:
    # a synthetic lap with sharp braking zones ~ real corners, ~4000 m
    d = np.arange(0, 4000, 10.0)
    speed = np.full_like(d, 320.0)
    for apex in (800, 1600, 2400, 3200):        # four corners
        speed -= 190 * np.exp(-((d - apex) ** 2) / (2 * 90.0 ** 2))  # narrow dips
    speed = np.clip(speed + offset_speed, 90, 340)
    throttle = np.clip((speed - 90) / 2.5, 0, 100)
    samples = [
        TelemetrySample(distance=float(x), speed=float(s), throttle=float(t),
                        brake=float(100 if s < 140 else 0), n_gear=int(min(8, s // 45)))
        for x, s, t in zip(d, speed, throttle)
    ]
    return TelemetryTrace(session_key=session_key, driver_number=driver_number,
                          lap_number=1, lap_time=92.0, samples=samples)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openapi_lists_all_routes():
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/api/sessions", "/api/telemetry", "/api/analysis/compare",
              "/api/tires/degradation", "/api/ai/report"):
        assert p in paths, f"missing route {p}"


def test_analysis_engine_produces_delta_and_corners():
    a = _fake_trace(offset_speed=+4, driver_number=1)
    b = _fake_trace(offset_speed=0, driver_number=44)
    cmp: LapComparison = analysis.compare_traces(
        Driver(driver_number=1, name_acronym="VER"),
        Driver(driver_number=44, name_acronym="HAM"),
        a, b, include_traces=False,
    )
    assert cmp.delta_trace, "delta trace should not be empty"
    assert cmp.total_delta_s < 0, "faster driver should have negative total delta"
    assert len(cmp.corners) >= 1, "should detect at least one corner"


def test_ai_engineer_is_grounded_and_has_no_llm_key(monkeypatch):
    a = _fake_trace(+4, driver_number=1)
    b = _fake_trace(0, driver_number=44)
    cmp = analysis.compare_traces(
        Driver(driver_number=1, name_acronym="VER"),
        Driver(driver_number=44, name_acronym="HAM"),
        a, b, include_traces=False)
    report = asyncio.run(ai_engineer.build_report(AIReportRequest(comparison=cmp)))
    assert report.source == "template"      # no API key in test env
    assert report.grounded is True
    assert report.summary
