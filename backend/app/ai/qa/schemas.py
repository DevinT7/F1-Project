"""Phase 7E Q&A schemas: what the LLM returns (AnswerDraft) and what the API returns (QAAnswer)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import Bullet
from .intents import SUPPORTED

MAX_ANSWER_WORDS = 120
MAX_FOLLOW_UPS = 3

SupportedIntent = Literal["where_lost_time", "why_faster_in_sector", "tire_status", "focus_corners", "when_to_pit",
                          "cause_attribution", "corner_detail"]
assert set(SupportedIntent.__args__) == {i.value for i in SUPPORTED}  # type: ignore[attr-defined]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswerDraft(_Model):
    """The LLM's output. Follow-ups are intent names (rendered to questions server-side)."""
    answer: Bullet
    follow_up_suggestions: list[SupportedIntent] = Field(default_factory=list)


class FollowUp(_Model):
    intent: SupportedIntent
    question: str


class QAAnswer(_Model):
    conversation_id: str
    question: str
    intent: str
    entities: dict[str, list[str]]
    answer: Bullet
    follow_up_suggestions: list[FollowUp]
    generated_by: Literal["llm", "template", "router"]
    outcome: str
    prompt_version: str
    factsheet_hash: str
    retried: bool = False
    fallback_reason: str | None = None


def draft_json_schema() -> dict[str, object]:
    """Strict, inlined schema of AnswerDraft for structured-output modes."""
    bullet = {"type": "object", "additionalProperties": False, "required": ["text", "fact_ids"],
              "properties": {"text": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}}}
    return {"type": "object", "additionalProperties": False, "required": ["answer", "follow_up_suggestions"],
            "properties": {"answer": bullet,
                           "follow_up_suggestions": {"type": "array",
                                                     "items": {"type": "string", "enum": [i.value for i in SUPPORTED]}}}}
