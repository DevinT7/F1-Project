"""TEST ONLY: scripted fake LLMs the running server can use (RACEIQ_ENGINEER_FAKE_LLM).

    inject   every report and answer carries an ungrounded number ("0.35 s") and a fabricated
             cause ("tyre wear"); the classifier says where_lost_time. The end-to-end safety
             test (frontend/e2e/engineer.spec.ts) asserts neither ever reaches the DOM.

Never set in production: the server logs a warning at startup when it is.
"""
from __future__ import annotations

import json
from typing import Any

from .llm_client import LLMProviderError, LLMResult, Message, Usage

INJECTED_NUMBER = "0.35 s"
INJECTED_CAUSE = "tyre wear"


class InjectingFakeLLM:
    model = "fake-injector"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *, system: str, messages: list[Message], response_schema: dict[str, Any],
                 temperature: float, max_tokens: int, timeout_s: float) -> LLMResult:
        self.calls += 1
        props = response_schema.get("properties", {})
        bad = {"text": f"The driver lost {INJECTED_NUMBER} in Turn 1 because of {INJECTED_CAUSE}.",
               "fact_ids": ["LAP.time_loss_s"]}
        if "headline" in props:
            body: dict[str, Any] = {"headline": bad, "biggest_time_loss": [bad], "strongest_areas": [],
                                    "tire_notes": [bad], "driving_suggestions": [bad], "strategy_options": [],
                                    "data_limitations": []}
        elif "answer" in props:
            body = {"answer": bad, "follow_up_suggestions": ["focus_corners"]}
        elif "intent" in props:
            body = {"intent": "where_lost_time"}
        else:
            raise LLMProviderError("unknown schema")
        return LLMResult(json.dumps(body), body, Usage(1200, 200), 3, self.model, f"inject-{self.calls}")


FAKES = {"inject": InjectingFakeLLM}
