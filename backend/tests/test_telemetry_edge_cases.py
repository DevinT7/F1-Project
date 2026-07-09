"""
Phase 1 (Person 2): edge-case coverage for `telemetry.build_trace` and the
resample/common_grid pair, all offline. Each case is a hand-built fake client so
the failure mode under test is obvious from the test itself.

    cd backend && pytest -q tests/test_telemetry_edge_cases.py
"""
from __future__ import annotations

import asyncio
import math

import numpy as np

from app.services import telemetry as telemetry_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client

SESSION_KEY = 9159


class _MiniClient:
    """A tiny stand-in for OpenF1Client returning exactly the rows a test needs."""

    def __init__(self, laps: list[dict], car_data: list[dict]):
        self._laps = laps
        self._car_data = car_data

    async def laps(self, session_key, driver_number=None, lap_number=None):
        return list(self._laps)

    async def car_data(self, session_key, driver_number, date_gt=None, date_lt=None):
        return list(self._car_data)


def _run(coro):
    return asyncio.run(coro)


def _use(monkeypatch, client) -> None:
    monkeypatch.setattr(telemetry_svc, "get_client", lambda: client)


LAP_OK = {
    "session_key": SESSION_KEY, "driver_number": 1, "lap_number": 5,
    "date_start": "2023-09-17T12:00:00Z", "lap_duration": 90.0,
    "is_pit_out_lap": False,
}


def test_missing_date_start_yields_empty_trace(monkeypatch):
    lap = dict(LAP_OK, date_start=None)
    _use(monkeypatch, _MiniClient([lap], []))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == []
    assert trace.driver_number == 1


def test_pit_out_lap_is_filtered_out(monkeypatch):
    lap = dict(LAP_OK, is_pit_out_lap=True)
    _use(monkeypatch, _MiniClient([lap], []))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == []


def test_missing_or_zero_lap_duration_is_filtered_out(monkeypatch):
    lap_missing = dict(LAP_OK, lap_duration=None)
    lap_zero = dict(LAP_OK, lap_duration=0)
    _use(monkeypatch, _MiniClient([lap_missing, lap_zero], []))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == []


def test_empty_car_data_yields_empty_trace(monkeypatch):
    _use(monkeypatch, _MiniClient([LAP_OK], []))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == []
    assert trace.lap_number == 5
    assert trace.lap_time == 90.0


def test_single_sample_is_too_short_to_use(monkeypatch):
    rows = [{"date": "2023-09-17T12:00:00Z", "speed": 250}]
    _use(monkeypatch, _MiniClient([LAP_OK], rows))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == [], "a single point can't establish a distance channel"


def test_all_zero_or_missing_speed_never_moves(monkeypatch):
    rows = [
        {"date": "2023-09-17T12:00:00.0Z", "speed": None},
        {"date": "2023-09-17T12:00:00.5Z", "speed": 0},
        {"date": "2023-09-17T12:00:01.0Z", "speed": 0},
    ]
    _use(monkeypatch, _MiniClient([LAP_OK], rows))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples == [], "distance never advances -> not a usable lap"


def test_out_of_order_and_duplicate_timestamps_dont_raise_and_stay_monotonic(monkeypatch):
    rows = [
        {"date": "2023-09-17T12:00:01.0Z", "speed": 200},   # out of order
        {"date": "2023-09-17T12:00:00.0Z", "speed": 180},
        {"date": "2023-09-17T12:00:00.0Z", "speed": 190},   # duplicate timestamp
        {"date": "2023-09-17T12:00:02.0Z", "speed": 210},
        {"date": "2023-09-17T12:00:03.0Z", "speed": 220},
    ]
    _use(monkeypatch, _MiniClient([LAP_OK], rows))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples, "should still produce a usable trace"
    distances = [s.distance for s in trace.samples]
    assert distances == sorted(distances), "distance must be non-decreasing"


def test_nan_and_none_channel_values_dont_raise(monkeypatch):
    rows = [
        {"date": "2023-09-17T12:00:00.0Z", "speed": 200, "throttle": None,
         "brake": float("nan"), "n_gear": None, "rpm": "not-a-number", "drs": None},
        {"date": "2023-09-17T12:00:01.0Z", "speed": 210, "throttle": 80,
         "brake": 0, "n_gear": 6, "rpm": 11000, "drs": 1},
        {"date": "2023-09-17T12:00:02.0Z", "speed": 220, "throttle": 90,
         "brake": 0, "n_gear": 7, "rpm": 11500, "drs": 1},
    ]
    _use(monkeypatch, _MiniClient([LAP_OK], rows))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples
    first = trace.samples[0]
    assert first.throttle is None
    assert first.brake is None, "NaN should be coerced to None, not passed through"
    assert first.rpm is None


def test_well_formed_case_produces_expected_distance(monkeypatch):
    rows = [
        {"date": "2023-09-17T12:00:00.0Z", "speed": 200, "throttle": 60,
         "brake": 0, "n_gear": 5, "rpm": 10000, "drs": 0},
        {"date": "2023-09-17T12:00:01.0Z", "speed": 200, "throttle": 60,
         "brake": 0, "n_gear": 5, "rpm": 10000, "drs": 0},
    ]
    _use(monkeypatch, _MiniClient([LAP_OK], rows))
    trace = _run(telemetry_svc.build_trace(SESSION_KEY, 1, 5))
    assert trace.samples
    # 200 km/h for 1s ~= 55.56 m
    assert math.isclose(trace.samples[-1].distance, 55.56, abs_tol=0.1)


def test_common_grid_and_resample_have_no_nans_in_overlap(monkeypatch):
    """Uses the Phase 0 fixtures directly (already-built real-shaped traces)."""
    fake = FakeOpenF1Client()
    _use(monkeypatch, fake)

    async def _run_traces():
        driver_trace = await telemetry_svc.build_trace(SESSION_KEY, 55)
        ref_trace = await telemetry_svc.build_trace(SESSION_KEY, 4)
        return driver_trace, ref_trace

    driver_trace, ref_trace = _run(_run_traces())

    grid = telemetry_svc.common_grid(driver_trace, ref_trace, step_m=10.0)
    assert len(grid) > 50, "fixtures should share several hundred metres of overlap"

    a = telemetry_svc.resample_to_grid(driver_trace, grid)
    b = telemetry_svc.resample_to_grid(ref_trace, grid)
    for chan in ("speed", "throttle", "brake"):
        assert not np.isnan(a[chan]).any(), f"driver {chan} has NaNs in the overlap"
        assert not np.isnan(b[chan]).any(), f"reference {chan} has NaNs in the overlap"
