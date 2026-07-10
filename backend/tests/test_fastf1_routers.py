from __future__ import annotations

from fastapi.testclient import TestClient

from app.clients import fastf1_client
from app.main import app


class FakeFastF1Gateway:
    async def get_lap_telemetry(self, **kwargs):
        return {
            "driver": kwargs["driver"].upper(),
            "lap_number": kwargs["lap_number"],
            "session": {
                "year": kwargs["year"],
                "grand_prix": kwargs["grand_prix"],
                "type": kwargs["session_type"],
            },
            "telemetry": [{"distance": 0.0, "speed": 100, "throttle": 0, "brake": False, "gear": 1, "time": 0.0}],
            "meta": {"source": "fastf1", "cached": False, "points": 1},
        }

    async def compare_lap_telemetry(self, **kwargs):
        return {
            "driver_a": {"driver": kwargs["driver_a"].upper(), "lap_number": kwargs["lap_a"], "telemetry": []},
            "driver_b": {"driver": kwargs["driver_b"].upper(), "lap_number": kwargs["lap_b"], "telemetry": []},
            "delta": [{"distance": 0.0, "time_delta": 0.0}],
            "sector_summary": [],
            "meta": {"source": "fastf1", "cached": False},
        }


def test_lap_telemetry_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(fastf1_client, "get_client", lambda: FakeFastF1Gateway())
    client = TestClient(app)

    response = client.get(
        "/api/telemetry/lap",
        params={
            "year": 2024,
            "grand_prix": "Monaco",
            "session": "Race",
            "driver": "VER",
            "lap": 23,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["driver"] == "VER"
    assert body["meta"]["source"] == "fastf1"
    assert body["telemetry"][0]["distance"] == 0.0


def test_compare_telemetry_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(fastf1_client, "get_client", lambda: FakeFastF1Gateway())
    client = TestClient(app)

    response = client.get(
        "/api/telemetry/compare",
        params={
            "year": 2024,
            "grand_prix": "Monaco",
            "session": "Race",
            "driver_a": "VER",
            "lap_a": 23,
            "driver_b": "NOR",
            "lap_b": 22,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["driver_a"]["driver"] == "VER"
    assert body["driver_b"]["driver"] == "NOR"
    assert body["delta"] == [{"distance": 0.0, "time_delta": 0.0}]


def test_lap_telemetry_driver_error_maps_to_404(monkeypatch) -> None:
    class BrokenGateway:
        async def get_lap_telemetry(self, **kwargs):
            raise fastf1_client.FastF1DriverNotFoundError("Driver XXX was not found.")

    monkeypatch.setattr(fastf1_client, "get_client", lambda: BrokenGateway())
    client = TestClient(app)

    response = client.get(
        "/api/telemetry/lap",
        params={
            "year": 2024,
            "grand_prix": "Monaco",
            "session": "Race",
            "driver": "XXX",
            "lap": 23,
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
