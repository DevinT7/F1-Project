"""Export real /api/v1 responses for the frontend's MSW tests and Demo mode.

    cd backend && python -m scripts.export_frontend_fixtures

Runs the actual app in-process against the Parquet fixture repository (2024 R5
Qualifying, VER/PER/ALO) and writes each response body to
frontend/src/fixtures/api/<name>.json (MSW tests + Demo mode). The 2024 schedule comes from FastF1 (one
network call, then cached in a temp store). Responses are never hand-written, so the
frontend tests always see exactly what the API returns.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import deps
from app.api.services import ComparisonService
from app.main import app
from app.storage.repository import ParquetRepository

BACKEND = Path(__file__).resolve().parents[1]
FIXTURE_STORE = BACKEND / "tests" / "fixtures" / "ingestion" / "store"
OUT = BACKEND.parent / "frontend" / "src" / "fixtures" / "api"
KEY = "2024-05-Q"
DEMO_RESOLUTION_M = 4.0  # ~1,350 points: small enough to ship, dense enough to look real

REQUESTS: list[tuple[str, str, str, dict[str, object] | None]] = [
    ("seasons", "GET", "/api/v1/seasons", None),
    ("events_2024", "GET", "/api/v1/seasons/2024/events", None),
    ("session_2024-05-Q", "GET", f"/api/v1/sessions/{KEY}", None),
    ("session_2024-06-Q", "GET", "/api/v1/sessions/2024-06-Q", None),
    ("drivers", "GET", f"/api/v1/sessions/{KEY}/drivers", None),
    ("laps", "GET", f"/api/v1/sessions/{KEY}/laps", None),
    ("fastest_laps", "GET", f"/api/v1/sessions/{KEY}/fastest-laps", None),
    ("corners", "GET", f"/api/v1/sessions/{KEY}/corners", None),
    ("comparison_VER_ALO", "POST", "/api/v1/comparisons", {
        "session_key": KEY, "reference": {"driver": "VER", "lap": "fastest"},
        "target": {"driver": "ALO", "lap": "fastest"}, "resolution_m": DEMO_RESOLUTION_M,
    }),
    ("error_lap_invalid", "POST", "/api/v1/comparisons", {
        "session_key": KEY, "reference": {"driver": "VER", "lap": "fastest"},
        "target": {"driver": "ALO", "lap": 1}, "resolution_m": DEMO_RESOLUTION_M,
    }),
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "store"
        shutil.copytree(FIXTURE_STORE, store)  # the schedule cache gets written here, not into the fixture
        repo = ParquetRepository(store)
        app.dependency_overrides[deps.get_repository] = lambda: repo
        app.dependency_overrides[deps.get_comparison_service] = lambda: ComparisonService(repo)
        OUT.mkdir(parents=True, exist_ok=True)
        with TestClient(app) as client:
            for name, method, path, body in REQUESTS:
                response = client.request(method, path, json=body, headers={"X-Request-ID": f"fixture-{name}"})
                payload = response.json()
                if name.startswith("comparison"):
                    payload["meta"]["cached"] = False
                compact = name.startswith("comparison")  # the big one ships compact
                text = json.dumps(payload, separators=(",", ":")) if compact else json.dumps(payload, indent=1)
                (OUT / f"{name}.json").write_text(text + "\n", encoding="utf-8")
                print(f"{response.status_code} {method} {path} -> {name}.json")
        app.dependency_overrides.clear()


if __name__ == "__main__":
    main()
