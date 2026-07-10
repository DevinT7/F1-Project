from __future__ import annotations

import asyncio

import pytest

from app.cache import InMemoryCache
from app.clients import fastf1_client


class FakeTelemetry(list):
    def add_distance(self):
        return self


class FakeLap(dict):
    def __init__(self, telemetry=None, **kwargs):
        super().__init__(**kwargs)
        self._telemetry = FakeTelemetry(telemetry or [])

    def get_car_data(self):
        return self._telemetry


class FakeSession:
    def __init__(self, laps):
        self.laps = laps
        self.session_key = 123


def _run(coro):
    return asyncio.run(coro)


def _telemetry_points(count: int = 5, *, offset_time: float = 0.0) -> list[dict]:
    return [
        {
            "Distance": float(i * 10),
            "Speed": 100.0 + i,
            "Throttle": float(i % 101),
            "Brake": i % 3 == 0,
            "nGear": 1 + (i % 8),
            "Time": offset_time + i * 0.5,
        }
        for i in range(count)
    ]


def _lap(driver: str = "VER", lap_number: int = 23, telemetry_count: int = 5) -> FakeLap:
    return FakeLap(
        telemetry=_telemetry_points(telemetry_count),
        Driver=driver,
        DriverNumber=1 if driver == "VER" else 4,
        LapNumber=lap_number,
        LapTime=75.0,
        Sector1Time=23.456,
        Sector2Time=24.0,
        Sector3Time=27.544,
    )


def test_telemetry_normalization() -> None:
    points = fastf1_client.normalize_telemetry_rows(_telemetry_points(3), "high")

    assert points == [
        {"distance": 0.0, "speed": 100.0, "throttle": 0.0, "brake": True, "gear": 1, "time": 0.0},
        {"distance": 10.0, "speed": 101.0, "throttle": 1.0, "brake": False, "gear": 2, "time": 0.5},
        {"distance": 20.0, "speed": 102.0, "throttle": 2.0, "brake": False, "gear": 3, "time": 1.0},
    ]


def test_downsampling_limits_points_and_keeps_last() -> None:
    points = [{"distance": float(i), "time": float(i)} for i in range(1200)]

    sampled = fastf1_client.downsample_telemetry(points, "low")

    assert len(sampled) <= 401
    assert sampled[-1] == points[-1]


def test_distance_alignment_and_delta_calculation() -> None:
    telemetry_a = [
        {"distance": 0.0, "time": 0.0},
        {"distance": 50.0, "time": 5.0},
        {"distance": 100.0, "time": 10.0},
    ]
    telemetry_b = [
        {"distance": 0.0, "time": 0.0},
        {"distance": 50.0, "time": 6.0},
        {"distance": 100.0, "time": 12.0},
    ]

    delta = fastf1_client.align_telemetry_delta(telemetry_a, telemetry_b, "high")

    assert delta[0] == {"distance": 0.0, "time_delta": 0.0}
    assert delta[-1] == {"distance": 100.0, "time_delta": -2.0}


def test_invalid_driver(monkeypatch) -> None:
    monkeypatch.setattr(
        fastf1_client,
        "_load_session_sync",
        lambda year, gp, session: FakeSession([_lap("VER", 23)]),
    )
    client = fastf1_client.FastF1Client(cache_backend=InMemoryCache(default_ttl_seconds=60))

    async def call():
        await client.get_lap_telemetry(2024, "Monaco", "Race", "NOR", 23)

    with pytest.raises(fastf1_client.FastF1DriverNotFoundError):
        _run(call())


def test_invalid_lap(monkeypatch) -> None:
    monkeypatch.setattr(
        fastf1_client,
        "_load_session_sync",
        lambda year, gp, session: FakeSession([_lap("VER", 23)]),
    )
    client = fastf1_client.FastF1Client(cache_backend=InMemoryCache(default_ttl_seconds=60))

    async def call():
        await client.get_lap_telemetry(2024, "Monaco", "Race", "VER", 99)

    with pytest.raises(fastf1_client.FastF1LapNotFoundError):
        _run(call())


def test_cache_hit_skips_fastf1_load(monkeypatch) -> None:
    calls = 0

    def fake_load(year, gp, session):
        nonlocal calls
        calls += 1
        return FakeSession([_lap("VER", 23)])

    monkeypatch.setattr(fastf1_client, "_load_session_sync", fake_load)
    client = fastf1_client.FastF1Client(cache_backend=InMemoryCache(default_ttl_seconds=60))

    async def call_twice():
        first = await client.get_lap_telemetry(2024, "Monaco", "Race", "VER", 23)
        second = await client.get_lap_telemetry(2024, "Monaco", "Race", "VER", 23)
        return first, second

    first, second = _run(call_twice())

    assert calls == 1
    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True


def test_compare_lap_telemetry_response(monkeypatch) -> None:
    laps = [
        _lap("VER", 23),
        FakeLap(
            telemetry=_telemetry_points(5, offset_time=0.2),
            Driver="NOR",
            DriverNumber=4,
            LapNumber=22,
            LapTime=76.0,
            Sector1Time=23.612,
            Sector2Time=24.1,
            Sector3Time=28.288,
        ),
    ]
    monkeypatch.setattr(
        fastf1_client,
        "_load_session_sync",
        lambda year, gp, session: FakeSession(laps),
    )
    client = fastf1_client.FastF1Client(cache_backend=InMemoryCache(default_ttl_seconds=60))

    result = _run(
        client.compare_lap_telemetry(2024, "Monaco", "Race", "VER", 23, "NOR", 22)
    )

    assert result["driver_a"]["driver"] == "VER"
    assert result["driver_b"]["driver"] == "NOR"
    assert result["delta"]
    assert result["sector_summary"][0]["delta"] == round(23.456 - 23.612, 3)
