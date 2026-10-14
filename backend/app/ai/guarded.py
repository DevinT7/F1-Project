"""The 7D guardrail loop, shared by reports (7D) and Q&A answers (7E).

    run_guarded(client, system=, messages=, schema=, check=, retry_message=, ...) -> GuardedRun

    attempt 1: LLM → check (parse + validate)         ok → llm_first_try
    attempt 2: same conversation + the validator's feedback (rule names and offending spans only)
                                                       ok → llm_after_retry
    still invalid                                      → outcome template_validation (value None)
    timeout / provider failure (after the client's own transient retries) → template_provider_error

The caller supplies `check` (strict parse + its validator) and builds the template when no value
comes back. Every attempt is logged as `<event>_attempt` with the prompt version, model, tokens,
latency and the violated rules.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

import structlog

from .llm_client import LLMClient, LLMError, LLMResult, Message
from .validator import ValidationResult, Violation, explain_for_retry

log = structlog.get_logger("raceiq.ai")
T = TypeVar("T")


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
class GuardedRun(Generic[T]):
    value: T | None                  # the validated value, or None when the caller must use its template
    outcome: Outcome
    reason: str | None               # fallback reason (rule names or "provider error: <Exc>")
    attempts: list[Attempt] = field(default_factory=list)
    first_attempt_violations: list[Violation] = field(default_factory=list)


Check = Callable[[LLMResult, bool], "tuple[T | None, ValidationResult]"]   # (result, is_retry)


def run_guarded(client: LLMClient, *, system: str, messages: list[Message], schema: dict[str, object],
                check: Callable[[LLMResult, bool], tuple[T | None, ValidationResult]],
                retry_message: Callable[[str], str], prompt_version: str, event: str,
                temperature: float, max_tokens: int, timeout_s: float) -> GuardedRun[T]:
    attempts: list[Attempt] = []
    first: list[Violation] = []
    msgs = list(messages)
    for number in (1, 2):
        start = time.perf_counter()
        try:
            res = client.generate(system=system, messages=msgs, response_schema=schema, temperature=temperature,
                                  max_tokens=max_tokens, timeout_s=timeout_s)
        except LLMError as exc:
            latency = int((time.perf_counter() - start) * 1000)
            attempts.append(Attempt(number, None, 0, 0, latency, False, (), type(exc).__name__))
            log.warning(f"{event}_attempt", prompt_version=prompt_version, attempt=number, outcome="provider_error",
                        error=type(exc).__name__, latency_ms=latency)
            return GuardedRun(None, Outcome.TEMPLATE_PROVIDER_ERROR, f"provider error: {type(exc).__name__}",
                              attempts, first)
        value, validation = check(res, number == 2)
        rules = ("unparseable",) if res.parsed is None else tuple(sorted({v.rule for v in validation.violations}))
        attempts.append(Attempt(number, res.model, res.usage.input_tokens, res.usage.output_tokens, res.latency_ms,
                                validation.ok, rules))
        log.info(f"{event}_attempt", prompt_version=prompt_version, model=res.model, attempt=number,
                 tokens_in=res.usage.input_tokens, tokens_out=res.usage.output_tokens, latency_ms=res.latency_ms,
                 outcome="valid" if validation.ok else "invalid", rules=list(rules))
        if number == 1:
            first = list(validation.violations)
        if validation.ok and value is not None:
            return GuardedRun(value, Outcome.LLM_FIRST_TRY if number == 1 else Outcome.LLM_AFTER_RETRY, None,
                              attempts, first)
        if number == 1:
            # the failed text is shown back to the model in THIS exchange only; callers never store it
            msgs = [*msgs, Message("assistant", res.text or "{}"),
                    Message("user", retry_message(explain_for_retry(validation.violations)))]
        else:
            return GuardedRun(None, Outcome.TEMPLATE_VALIDATION, ",".join(rules) or "invalid", attempts, first)
    raise AssertionError("unreachable")
