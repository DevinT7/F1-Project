from __future__ import annotations

from fastapi.testclient import TestClient

from app.cache import InMemoryCache
from app.main import app


def test_app_starts_and_docs_load() -> None:
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200


def test_api_health_contract() -> None:
    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["cache"] == "ok"
    assert body["timestamp"].endswith("Z")
    assert body["version"]


def test_cors_headers_for_local_frontend() -> None:
    client = TestClient(app)
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_in_memory_cache_set_get_delete() -> None:
    cache = InMemoryCache(default_ttl_seconds=60)

    cache.set("foundation:test", {"value": 1})

    assert cache.get("foundation:test") == {"value": 1}
    assert cache.delete("foundation:test") is True
    assert cache.get("foundation:test") is None


def test_router_registration() -> None:
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]

    for path in (
        "/api/health",
        "/api/sessions",
        "/api/drivers",
        "/api/laps",
        "/api/telemetry",
        "/api/telemetry/status",
        "/api/stints",
        "/api/weather",
    ):
        assert path in paths


def test_structured_404_response() -> None:
    client = TestClient(app)
    response = client.get("/api/not-a-real-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_structured_validation_response() -> None:
    client = TestClient(app)
    response = client.get("/api/analysis/compare")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
