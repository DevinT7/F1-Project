"""Phase 7E orchestration: route → slice → (LLM → validate → retry once → template) → history.

    answer_question(factsheet, question, client=None, store=DEFAULT_STORE, conversation_id=None) -> QAResult

  out_of_scope (rule or classifier)   → refusal listing what can be discussed     outcome "refused" (no LLM)
  a named corner/sector not in the FactSheet → a clarification                  outcome "clarification" (no LLM)
  no client (no LLM configured)       → the per-intent template                 outcome "template_no_llm"
  otherwise the 7D guardrail loop (guarded.run_guarded) with the Q&A schema and validator:
      llm_first_try | llm_after_retry | template_validation | template_provider_error

Only the returned (validated) answer enters the conversation history. Nothing unvalidated is
returned: the final answer, template included, is asserted valid.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import structlog

from ..guarded import Attempt, run_guarded
from ..llm_client import LLMClient, LLMResult, Message
from ..report_generator import parse_strict
from ..schemas import Bullet, FactSheet
from ..validator import ValidationResult, Violation
from .conversation import DEFAULT_STORE, ConversationStore, Turn
from .intents import Entities, Intent, Route, route
from .prompts import PROMPT_VERSION, retry_message, system_prompt, user_message
from .schemas import AnswerDraft, FollowUp, QAAnswer, draft_json_schema
from .slices import Slice, build_slice
from .templates import clarification, follow_ups, refusal, template_answer
from .validation import validate_answer

log = structlog.get_logger("raceiq.ai")

TEMPERATURE = 0.2
MAX_TOKENS = 600
TIMEOUT_S = 30.0
TEMPLATE_VERSION = "qa-template-v1"


@dataclass
class QAResult:
    answer: QAAnswer
    outcome: str
    route: Route
    attempts: list[Attempt] = field(default_factory=list)
    first_attempt_violations: list[Violation] = field(default_factory=list)

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(a.tokens_in for a in self.attempts), sum(a.tokens_out for a in self.attempts)

    @property
    def latency_ms(self) -> int:
        return sum(a.latency_ms for a in self.attempts)


def _check(res: LLMResult, fs: FactSheet, slc: Slice) -> tuple[AnswerDraft | None, ValidationResult]:
    draft, problems = parse_strict(res, AnswerDraft)
    if draft is None:
        return None, ValidationResult(False, problems)
    result = validate_answer(draft.answer, draft.follow_up_suggestions, fs, slc)
    return draft, result


def answer_question(fs: FactSheet, question: str, *, client: LLMClient | None = None,
                    store: ConversationStore = DEFAULT_STORE, conversation_id: str | None = None) -> QAResult:
    cid = conversation_id or store.new_id()
    conv = store.get(cid, fs.factsheet_hash)
    r = route(question, fs, previous=conv.previous(), client=client)
    attempts: list[Attempt] = []
    first: list[Violation] = []
    retried = False
    reason: str | None = None
    suggestions: list[Intent] = []
    slc = build_slice(r.intent, fs, r.entities)

    if r.intent is Intent.OUT_OF_SCOPE:
        bullet, outcome, by = refusal(r.out_of_scope), "refused", "router"
    elif r.entities.unknown and not r.entities.segments:
        bullet, outcome, by = clarification(fs, r.entities.unknown), "clarification", "router"
    elif r.intent in (Intent.WHY_FASTER_IN_SECTOR,) and not r.entities.sectors or \
            r.intent is Intent.CORNER_DETAIL and not r.entities.segments:
        bullet, outcome, by = _which(r.intent), "clarification", "router"
    elif client is None:
        bullet, outcome, by = template_answer(r.intent, fs, slc, question), "template_no_llm", "template"
    else:
        messages = [*conv.history(), Message("user", user_message(question, r.intent, r.entities, fs, slc))]
        if os.environ.get("RACEIQ_AI_DEBUG_LOG_FACTSHEET") == "1":
            log.debug("ai.qa_prompt", factsheet_hash=fs.factsheet_hash, user_message=messages[-1].content)
        run = run_guarded(client, system=system_prompt(), messages=messages, schema=draft_json_schema(),
                          check=lambda res, _retry: _check(res, fs, slc), retry_message=retry_message,
                          prompt_version=PROMPT_VERSION, event="ai.qa", temperature=TEMPERATURE,
                          max_tokens=MAX_TOKENS, timeout_s=TIMEOUT_S)
        attempts, first = run.attempts, run.first_attempt_violations
        outcome = run.outcome.value
        if run.value is not None:
            bullet, by = run.value.answer, "llm"
            suggestions = [Intent(s) for s in run.value.follow_up_suggestions]
            retried = outcome == "llm_after_retry"
        else:
            bullet, by, reason = template_answer(r.intent, fs, slc, question), "template", run.reason

    final = validate_answer(bullet, [], fs, slc if by != "router" else build_slice(Intent.OUT_OF_SCOPE, fs, Entities()))
    assert final.ok, f"refusing to return an invalid answer: {[(v.rule, v.detail) for v in final.violations]}"
    ups: list[FollowUp] = follow_ups(suggestions, fs, r.intent)
    answer = QAAnswer(conversation_id=cid, question=question, intent=r.intent.value, entities=r.entities.as_dict(),
                      answer=bullet, follow_up_suggestions=ups, generated_by=by, outcome=outcome,  # type: ignore[arg-type]
                      prompt_version=PROMPT_VERSION if by == "llm" else TEMPLATE_VERSION,
                      factsheet_hash=fs.factsheet_hash, retried=retried, fallback_reason=reason)
    store.append(cid, fs.factsheet_hash, Turn(question, r.intent, r.entities, bullet.text, tuple(bullet.fact_ids)))
    log.info("ai.qa", prompt_version=PROMPT_VERSION, intent=r.intent.value, route_method=r.method,
             route_confidence=r.confidence, outcome=outcome, fallback_reason=reason, attempts=len(attempts),
             tokens_in=sum(a.tokens_in for a in attempts), tokens_out=sum(a.tokens_out for a in attempts))
    return QAResult(answer, outcome, r, attempts, first)


def _which(intent: Intent) -> Bullet:
    noun = "sector" if intent is Intent.WHY_FASTER_IN_SECTOR else "corner"
    return Bullet(text=f"Which {noun} do you mean? Name it by its number.", fact_ids=[])
