"""Phase 7D: report generation inside the guardrail cage.

    generate_report(factsheet, client) -> GenerationResult(report, outcome, fallback_reason, attempts)

    attempt 1: LLM → parse JSON → validate        ok → llm_first_try
    attempt 2: same conversation + the validator's feedback (rule names and offending spans only)
                                                    ok → llm_after_retry (report.retried = True)
    still invalid                                   → template (template_validation, fallback_reason = rule names)
    timeout / provider failure (after the client's transient retries) → template (template_provider_error)

The loop itself is app/ai/guarded.py (shared with 7E Q&A). Nothing unvalidated is ever returned:
the final report, template included, is asserted valid. Every call is logged with the prompt
version, model, tokens, latency and outcome; the FactSheet itself is logged only at DEBUG and only
when RACEIQ_AI_DEBUG_LOG_FACTSHEET=1.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from .fallback import build_template_report
from .guarded import Attempt, Outcome, run_guarded
from .llm_client import LLMClient, LLMResult, Message
from .prompts import PROMPT_VERSION, response_schema, retry_message, system_prompt, user_message
from .schemas import FactSheet, Report, ReportDraft
from .validator import ValidationResult, Violation, validate_report

__all__ = ["Attempt", "GenerationResult", "Outcome", "generate_report"]

log = structlog.get_logger("raceiq.ai")
M = TypeVar("M", bound=BaseModel)

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2500
DEFAULT_TIMEOUT_S = 60.0


@dataclass
class GenerationResult:
    report: Report
    outcome: Outcome
    fallback_reason: str | None
    attempts: list[Attempt] = field(default_factory=list)
    first_attempt_violations: list[Violation] = field(default_factory=list)

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(a.tokens_in for a in self.attempts), sum(a.tokens_out for a in self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


def parse_strict(result: LLMResult, model: type[M]) -> tuple[M | None, list[Violation]]:
    """Strict parse into a draft model; unparseable or schema-invalid output becomes schema violations."""
    if result.parsed is None:
        return None, [Violation("schema", None, None, "the response was not a single JSON object")]
    try:
        return model.model_validate(result.parsed), []
    except ValidationError as exc:
        return None, [Violation("schema", None, None, f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
                      for e in exc.errors()[:8]]


def _to_report(result: LLMResult, fs: FactSheet, retried: bool) -> tuple[Report | None, ValidationResult]:
    draft, problems = parse_strict(result, ReportDraft)
    if draft is None:
        return None, ValidationResult(False, problems)
    report = Report(**draft.model_dump(), generated_by="llm", prompt_version=PROMPT_VERSION,
                    factsheet_hash=fs.factsheet_hash, retried=retried)
    return report, validate_report(report, fs)


def generate_report(fs: FactSheet, client: LLMClient, *, temperature: float = DEFAULT_TEMPERATURE,
                    max_tokens: int = DEFAULT_MAX_TOKENS, timeout_s: float = DEFAULT_TIMEOUT_S) -> GenerationResult:
    messages = [Message("user", user_message(fs))]
    if os.environ.get("RACEIQ_AI_DEBUG_LOG_FACTSHEET") == "1":
        log.debug("ai.factsheet", factsheet_hash=fs.factsheet_hash, user_message=messages[0].content)
    run = run_guarded(client, system=system_prompt(), messages=messages, schema=response_schema(),
                      check=lambda res, retried: _to_report(res, fs, retried), retry_message=retry_message,
                      prompt_version=PROMPT_VERSION, event="ai.report", temperature=temperature,
                      max_tokens=max_tokens, timeout_s=timeout_s)
    if run.value is not None:
        result = GenerationResult(run.value, run.outcome, None, run.attempts, run.first_attempt_violations)
    else:
        report = build_template_report(fs).model_copy(update={"fallback_reason": run.reason})
        result = GenerationResult(report, run.outcome, run.reason, run.attempts, run.first_attempt_violations)
    final = validate_report(result.report, fs)
    assert final.ok, f"refusing to return an invalid report: {[v.rule for v in final.violations]}"
    log.info("ai.report", prompt_version=PROMPT_VERSION, outcome=result.outcome.value,
             fallback_reason=result.fallback_reason, attempts=len(result.attempts),
             tokens_in=result.tokens[0], tokens_out=result.tokens[1], latency_ms=result.latency_ms)
    return result
