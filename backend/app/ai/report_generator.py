"""Phase 7D: generation inside the guardrail cage.

    generate_report(factsheet, client) -> GenerationResult(report, outcome, fallback_reason, attempts)

    attempt 1: LLM → parse JSON → validate        ok → llm_first_try
    attempt 2: same conversation + the validator's feedback (rule names and offending spans only)
                                                    ok → llm_after_retry (report.retried = True)
    still invalid                                   → template (template_validation, fallback_reason = rule names)
    timeout / provider failure (after the client's transient retries) → template (template_provider_error)

Nothing unvalidated is ever returned: the final report, template included, is asserted valid.
Every call is logged with the prompt version, model, tokens, latency and outcome; the FactSheet
itself is logged only at DEBUG and only when RACEIQ_AI_DEBUG_LOG_FACTSHEET=1.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum

import structlog
from pydantic import ValidationError

from .fallback import build_template_report
from .llm_client import LLMClient, LLMError, LLMResult, Message
from .prompts import PROMPT_VERSION, response_schema, retry_message, system_prompt, user_message
from .schemas import FactSheet, Report, ReportDraft
from .validator import ValidationResult, Violation, explain_for_retry, validate_report

log = structlog.get_logger("raceiq.ai")

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 2500
DEFAULT_TIMEOUT_S = 60.0


class Outcome(str, Enum):
    LLM_FIRST_TRY = "llm_first_try"
    LLM_AFTER_RETRY = "llm_after_retry"
    TEMPLATE_VALIDATION = "template_validation"
    TEMPLATE_PROVIDER_ERROR = "template_provider_error"


@dataclass(frozen=True)
class Attempt:
    number: int
    model: str | None
    tokens_in: int
    tokens_out: int
    latency_ms: int
    ok: bool
    rules: tuple[str, ...]           # violated rules ("unparseable" for non-JSON output)
    error: str | None = None


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


def _to_report(result: LLMResult, fs: FactSheet, retried: bool) -> tuple[Report | None, ValidationResult]:
    """Parse strictly, then validate. Unparseable or schema-invalid output is a validation failure."""
    data = result.parsed
    if data is None:
        v = Violation("schema", None, None, "the response was not a single JSON object")
        return None, ValidationResult(False, [v])
    try:
        draft = ReportDraft.model_validate(data)
    except ValidationError as exc:
        details = [Violation("schema", None, None, f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}")
                   for e in exc.errors()[:8]]
        return None, ValidationResult(False, details)
    report = Report(**draft.model_dump(), generated_by="llm", prompt_version=PROMPT_VERSION,
                    factsheet_hash=fs.factsheet_hash, retried=retried)
    return report, validate_report(report, fs)


def _template(fs: FactSheet, outcome: Outcome, reason: str, attempts: list[Attempt],
              first: list[Violation]) -> GenerationResult:
    report = build_template_report(fs).model_copy(update={"fallback_reason": reason})
    return GenerationResult(report, outcome, reason, attempts, first)


def generate_report(fs: FactSheet, client: LLMClient, *, temperature: float = DEFAULT_TEMPERATURE,
                    max_tokens: int = DEFAULT_MAX_TOKENS, timeout_s: float = DEFAULT_TIMEOUT_S) -> GenerationResult:
    system = system_prompt()
    messages = [Message("user", user_message(fs))]
    if os.environ.get("RACEIQ_AI_DEBUG_LOG_FACTSHEET") == "1":
        log.debug("ai.factsheet", factsheet_hash=fs.factsheet_hash, user_message=messages[0].content)
    attempts: list[Attempt] = []
    first: list[Violation] = []
    result_out: GenerationResult | None = None
    for number in (1, 2):
        start = time.perf_counter()
        try:
            res = client.generate(system=system, messages=messages, response_schema=response_schema(),
                                  temperature=temperature, max_tokens=max_tokens, timeout_s=timeout_s)
        except LLMError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            attempts.append(Attempt(number, None, 0, 0, latency, False, (), type(exc).__name__))
            log.warning("ai.report_attempt", prompt_version=PROMPT_VERSION, attempt=number, outcome="provider_error",
                        error=type(exc).__name__, latency_ms=latency)
            result_out = _template(fs, Outcome.TEMPLATE_PROVIDER_ERROR, f"provider error: {type(exc).__name__}",
                                   attempts, first)
            break
        report, validation = _to_report(res, fs, retried=number == 2)
        rules = tuple(sorted({v.rule for v in validation.violations}))
        if res.parsed is None:
            rules = ("unparseable",)
        attempts.append(Attempt(number, res.model, res.usage.input_tokens, res.usage.output_tokens, res.latency_ms,
                                validation.ok, rules))
        log.info("ai.report_attempt", prompt_version=PROMPT_VERSION, model=res.model, attempt=number,
                 tokens_in=res.usage.input_tokens, tokens_out=res.usage.output_tokens, latency_ms=res.latency_ms,
                 outcome="valid" if validation.ok else "invalid", rules=list(rules))
        if number == 1:
            first = list(validation.violations)
        if validation.ok and report is not None:
            outcome = Outcome.LLM_FIRST_TRY if number == 1 else Outcome.LLM_AFTER_RETRY
            result_out = GenerationResult(report, outcome, None, attempts, first)
            break
        if number == 1:
            messages = [*messages, Message("assistant", res.text or "{}"),
                        Message("user", retry_message(explain_for_retry(validation.violations)))]
        else:
            result_out = _template(fs, Outcome.TEMPLATE_VALIDATION, ",".join(rules) or "invalid", attempts, first)
    assert result_out is not None
    final = validate_report(result_out.report, fs)
    assert final.ok, f"refusing to return an invalid report: {[v.rule for v in final.violations]}"
    log.info("ai.report", prompt_version=PROMPT_VERSION, outcome=result_out.outcome.value,
             fallback_reason=result_out.fallback_reason, attempts=len(attempts),
             tokens_in=result_out.tokens[0], tokens_out=result_out.tokens[1], latency_ms=result_out.latency_ms)
    return result_out
