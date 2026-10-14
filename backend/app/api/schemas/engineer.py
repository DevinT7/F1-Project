"""Phase 7F /api/v1/engineer request and response schemas.

Reports and answers carry everything the UI needs to be transparent: who wrote the text
(generated_by), the prompt version, the FactSheet hash, the outcome and fallback reason, every
cited fact (for hover) and the fact prefix → finding id map (for segment highlighting).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .common import SESSION_KEY_DESCRIPTION, canonical_session_key
from .comparisons import LapSelector

MAX_QUESTION_CHARS = 300
CONVERSATION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
FACTSHEET_HASH_PATTERN = r"^[0-9a-f]{64}$"


def sanitize_question(text: str) -> str:
    """Strip control and format characters (incl. zero-width and bidi overrides), collapse
    whitespace. The result only ever goes into a user message, never into a system prompt."""
    cleaned = "".join(" " if ch in "\t\n\r\v\f" else ch for ch in text)
    cleaned = "".join(ch for ch in cleaned if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return re.sub(r"\s+", " ", cleaned).strip()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TireContext(_Model):
    lap: int | None = Field(default=None, ge=1, le=200,
                            description="forecast 'as of' this lap (default: the target lap); laps after it are never used")
    horizon: int = Field(default=5, ge=1, le=25, description="laps to forecast")


class _ComparisonRef(_Model):
    session_key: str = Field(description=SESSION_KEY_DESCRIPTION)
    reference: LapSelector
    target: LapSelector

    @field_validator("session_key")
    @classmethod
    def _key(cls, value: str) -> str:
        canonical_session_key(value)
        return value


class EngineerReportRequest(_ComparisonRef):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "session_key": "2024-14-R", "reference": {"driver": "PIA", "lap": 39}, "target": {"driver": "LEC", "lap": 39},
        "tire_context": {"lap": 39, "horizon": 5}}]})
    tire_context: TireContext | None = Field(default=None, description="race only: include the target's tyre forecast")
    regenerate: bool = Field(default=False, description="bypass the report cache (the UI's regenerate button)")


class EngineerAskRequest(_ComparisonRef):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
        "session_key": "2024-05-Q", "reference": {"driver": "VER", "lap": "fastest"},
        "target": {"driver": "ALO", "lap": "fastest"}, "question": "Where did ALO lose the most time?"}]})
    question: str = Field(min_length=1, max_length=2000,
                          description=f"1–{MAX_QUESTION_CHARS} characters after control characters are stripped")
    conversation_id: str | None = Field(default=None, pattern=CONVERSATION_ID_PATTERN,
                                        description="continue a conversation (returned by the first answer)")
    tire_context: TireContext | None = None

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        cleaned = sanitize_question(value)
        if not cleaned:
            raise ValueError("question is empty after removing control characters")
        if len(cleaned) > MAX_QUESTION_CHARS:
            raise ValueError(f"question is {len(cleaned)} characters (max {MAX_QUESTION_CHARS})")
        return cleaned


class FactView(_Model):
    id: str
    value: float | int | str | bool | list[str] | None
    unit: str | None = Field(description="null for categorical facts")
    display: str | None = Field(description="the primary display string (numbers) or null")
    meaning: str


class BulletOut(_Model):
    text: str
    fact_ids: list[str]


class EngineerReportSections(_Model):
    headline: BulletOut
    biggest_time_loss: list[BulletOut]
    strongest_areas: list[BulletOut]
    tire_notes: list[BulletOut]
    driving_suggestions: list[BulletOut]
    strategy_options: list[BulletOut]
    data_limitations: list[BulletOut]


GeneratedBy = Literal["llm", "template", "router"]


class _Transparency(_Model):
    generated_by: GeneratedBy
    outcome: str = Field(description="llm_first_try | llm_after_retry | template_validation | template_provider_error | "
                                     "template_no_llm | template_disabled (kill switch) | refused | clarification")
    fallback_reason: str | None = Field(description="why a template replaced the LLM (rule names or provider error)")
    prompt_version: str
    factsheet_hash: str
    facts: dict[str, FactView] = Field(description="every cited fact, by id (hover / audit)")
    segments: dict[str, str] = Field(description="fact prefix (T6, STR2, SEC1) → finding id, for highlighting")


class EngineerReport(_Transparency):
    report: EngineerReportSections
    retried: bool
    cached: bool
    model: str | None = Field(description="LLM model name, null for template reports")


class FollowUpOut(_Model):
    intent: str
    question: str


class EngineerAnswer(_Transparency):
    conversation_id: str
    question: str = Field(description="the sanitized question")
    intent: str
    entities: dict[str, list[str]]
    answer: BulletOut
    follow_up_suggestions: list[FollowUpOut]
    retried: bool


class FactsResponse(_Model):
    factsheet_hash: str
    factsheet_version: str
    session: dict[str, Any]
    drivers: dict[str, Any]
    facts: list[FactView]
