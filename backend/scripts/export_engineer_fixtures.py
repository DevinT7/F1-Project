"""Export real /api/v1/engineer responses for the frontend's MSW tests and Demo mode (Phase 7F).

    cd backend && python -m scripts.export_engineer_fixtures

Runs the app in-process against the Parquet fixture repository (2024 R5 Q, VER vs ALO) with no
LLM (template report and template answers), offline. Writes frontend/src/fixtures/api/engineer_*.json.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.ai.qa.conversation import ConversationStore
from app.api import deps
from app.api.engineer_service import EngineerService
from app.api.services import ComparisonService
from app.config import settings
from app.main import app
from app.storage.repository import ParquetRepository
from app.tires.registry import ModelRegistry
from app.tires.service import TireService

from scripts.export_frontend_fixtures import FIXTURE_STORE, KEY, OUT

PAIR = {"session_key": KEY, "reference": {"driver": "VER", "lap": "fastest"}, "target": {"driver": "ALO", "lap": "fastest"}}
REQUESTS = [
    ("engineer_report_VER_ALO", "/api/v1/engineer/report", PAIR),
    ("engineer_ask_T16", "/api/v1/engineer/ask", PAIR | {"question": "What happened in Turn 16?",
                                                          "conversation_id": "fixture-conversation"}),
    ("engineer_ask_cause", "/api/v1/engineer/ask", PAIR | {"question": "Was the time loss caused by the driver, "
                                                                       "the tyres or traffic?",
                                                            "conversation_id": "fixture-conversation-2"}),
    ("engineer_ask_refused", "/api/v1/engineer/ask", PAIR | {"question": "Who will win the championship?",
                                                              "conversation_id": "fixture-conversation-3"}),
]


def main() -> None:
    repo = ParquetRepository(FIXTURE_STORE)
    service = EngineerService(ComparisonService(repo), TireService(repo, ModelRegistry(settings.tire_registry_path)),
                              client_factory=lambda: None, llm_enabled=lambda: True, store=ConversationStore())
    app.dependency_overrides[deps.get_engineer_service] = lambda: service
    OUT.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        for name, path, body in REQUESTS:
            response = client.post(path, json=body, headers={"X-Request-ID": f"fixture-{name}"})
            payload = response.json()
            if "cached" in payload:
                payload["cached"] = False
            (OUT / f"{name}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{response.status_code} POST {path} -> {name}.json")
    app.dependency_overrides.clear()


if __name__ == "__main__":
    main()
