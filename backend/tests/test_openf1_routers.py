from __future__ import annotations

from fastapi.testclient import TestClient

from app.clients.openf1 import OpenF1Result
from app.main import app
from app.routers import drivers, laps, sessions, stints, weather


class _FakeOpenF1Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_sessions(self, **kwargs):
        self.calls.append(("sessions", kwargs))
        return OpenF1Result(
            data=[{"session_key": 1, "year": kwargs.get("year")}],
            cached=False,
            cache_ttl_seconds=86400,
            endpoint="sessions",
        )

    async def get_drivers(self, **kwargs):
        self.calls.append(("drivers", kwargs))
        return OpenF1Result(
            data=[{"driver_number": kwargs.get("driver_number") or 1}],
            cached=True,
            cache_ttl_seconds=21600,
            endpoint="drivers",
        )

    async def get_laps(self, **kwargs):
        self.calls.append(("laps", kwargs))
        return OpenF1Result(
            data=[{"lap_number": 1, "session_key": kwargs["session_key"]}],
            cached=False,
            cache_ttl_seconds=3600,
            endpoint="laps",
        )

    async def get_stints(self, **kwargs):
        self.calls.append(("stints", kwargs))
        return OpenF1Result(
            data=[{"stint_number": 1, "session_key": kwargs["session_key"]}],
            cached=False,
            cache_ttl_seconds=3600,
            endpoint="stints",
        )

    async def get_weather(self, **kwargs):
        self.calls.append(("weather", kwargs))
        return OpenF1Result(
            data=[{"air_temperature": 24.0, "session_key": kwargs["session_key"]}],
            cached=False,
            cache_ttl_seconds=1800,
            endpoint="weather",
        )


def _client_with_fake(monkeypatch, router_module):
    fake = _FakeOpenF1Gateway()
    monkeypatch.setattr(router_module, "get_client", lambda: fake)
    return TestClient(app), fake


def test_sessions_router_returns_enveloped_data(monkeypatch) -> None:
    client, fake = _client_with_fake(monkeypatch, sessions)

    response = client.get("/api/sessions", params={"year": 2024})

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == [{"session_key": 1, "year": 2024}]
    assert body["meta"]["source"] == "openf1"
    assert body["meta"]["cached"] is False
    assert body["meta"]["count"] == 1
    assert fake.calls == [
        (
            "sessions",
            {"year": 2024, "country_name": None, "session_type": None},
        )
    ]


def test_drivers_router_returns_enveloped_data(monkeypatch) -> None:
    client, fake = _client_with_fake(monkeypatch, drivers)

    response = client.get("/api/drivers", params={"session_key": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["data"] == [{"driver_number": 1}]
    assert body["meta"]["source"] == "cache"
    assert body["meta"]["cached"] is True
    assert fake.calls == [("drivers", {"session_key": 1, "driver_number": None})]


def test_laps_router_returns_enveloped_data(monkeypatch) -> None:
    client, fake = _client_with_fake(monkeypatch, laps)

    response = client.get("/api/laps", params={"session_key": 1, "driver_number": 44})

    assert response.status_code == 200
    assert response.json()["data"] == [{"lap_number": 1, "session_key": 1}]
    assert fake.calls == [("laps", {"session_key": 1, "driver_number": 44})]


def test_stints_router_returns_enveloped_data(monkeypatch) -> None:
    client, fake = _client_with_fake(monkeypatch, stints)

    response = client.get("/api/stints", params={"session_key": 1, "driver_number": 44})

    assert response.status_code == 200
    assert response.json()["data"] == [{"stint_number": 1, "session_key": 1}]
    assert fake.calls == [("stints", {"session_key": 1, "driver_number": 44})]


def test_weather_router_returns_enveloped_data(monkeypatch) -> None:
    client, fake = _client_with_fake(monkeypatch, weather)

    response = client.get("/api/weather", params={"session_key": 1})

    assert response.status_code == 200
    assert response.json()["data"] == [{"air_temperature": 24.0, "session_key": 1}]
    assert fake.calls == [("weather", {"session_key": 1})]


def test_missing_session_key_returns_useful_400() -> None:
    client = TestClient(app)

    response = client.get("/api/laps")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
