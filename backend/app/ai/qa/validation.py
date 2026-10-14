"""Phase 7E answer validation: the 7B per-bullet rules plus the Q&A rules.

    validate_answer(bullet, follow_ups, factsheet, slice) -> ValidationResult

  7B rules     every per-bullet rule (numbers grounded in cited facts, labels, units, directions,
               causal claims, unavailable data, scope, injection), with the answer limit of 120 words;
               pit-window answers must be hedged like strategy options
  off_topic    the answer cites at least one fact of the question's slice
  follow_ups   at most 3, each a supported intent
  qa_policy    cause_attribution only (tests/test_qa_policy.py):
                 * cites only the evidence's facts
                 * traffic unavailable → says traffic cannot be assessed and cites conditions.traffic_data
                 * "evidence suggests" wording when there are factors
                 * no single definitive cause unless exactly one factor and high data quality
"""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..schemas import Bullet, FactSheet
from ..validator import UNAVAILABLE_MARKER, ValidationResult, Violation, _check_bullet, _index
from .evidence import TRAFFIC_FACT
from .intents import SUPPORTED, Intent
from .schemas import MAX_ANSWER_WORDS, MAX_FOLLOW_UPS
from .slices import Slice

SECTION = "answer"
TYRE_WORDS = re.compile(r"tyres?|tires?|wear|degradation", re.IGNORECASE)
CONDITION_WORDS = re.compile(r"rain|flags?", re.IGNORECASE)
TRAFFIC_UNASSESSABLE = re.compile(
    r"\btraffic\b[^.;]*\b(?:cannot|can't|can not|could not|couldn't) be (?:assessed|judged|determined|evaluated|"
    r"quantified)\b|\btraffic (?:data )?(?:is |was )?(?:unavailable|not available|missing|unknown)\b|"
    r"\bno traffic data\b|\btraffic\b[^.;]*\b(?:data )?(?:is |was )?(?:unavailable|not available)\b", re.IGNORECASE)
EVIDENCE_WORDING = re.compile(r"\bevidence (?:suggests|points|indicates)\b", re.IGNORECASE)
DEFINITIVE = re.compile(
    r"\b(?:(?:was|is|were) (?:entirely |solely |purely |clearly |definitely )?caused by|the (?:sole|only|main|single|"
    r"real|definitive) (?:cause|reason)|the cause (?:was|is)|solely|entirely|purely|definitely|certainly|"
    r"undoubtedly|100%|without doubt|for sure)\b", re.IGNORECASE)


def validate_answer(bullet: Bullet, follow_ups: Sequence[str], fs: FactSheet, slc: Slice) -> ValidationResult:
    idx = _index(fs)
    ev = slc.evidence
    allowed_kw = None
    if ev is not None:
        words = [TYRE_WORDS.pattern] if ev.tire_factors else []
        words += [CONDITION_WORDS.pattern] if ev.conditions else []
        allowed_kw = re.compile("|".join(words), re.IGNORECASE) if words else None
    hedge = slc.intent is Intent.WHEN_TO_PIT and fs.tire.get("window_earliest_lap") is not None
    violations, _ = _check_bullet(bullet, SECTION, None, idx, max_words=MAX_ANSWER_WORDS, hedge_required=hedge,
                                  allowed_keywords=allowed_kw)

    def v(rule: str, detail: str) -> None:
        violations.append(Violation(rule, SECTION, None, detail))

    cited = set(bullet.fact_ids)
    if slc.ids and not cited & slc.ids:
        v("off_topic", "cites no fact relevant to the question")
    if len(follow_ups) > MAX_FOLLOW_UPS:
        v("schema", f"{len(follow_ups)} follow-up suggestions (max {MAX_FOLLOW_UPS})")
    bad = [f for f in follow_ups if f not in {i.value for i in SUPPORTED}]
    if bad:
        v("schema", f"follow-up suggestions must be supported intents: {bad}")

    if slc.intent is Intent.CAUSE_ATTRIBUTION and ev is not None:
        text = bullet.text
        outside = sorted(cited - slc.ids)
        if outside:
            v("qa_policy", f"cites facts outside the evidence: {outside[:4]}")
        if ev.traffic == "unavailable":
            if not (TRAFFIC_UNASSESSABLE.search(text) or (re.search(r"\btraffic\b", text, re.I)
                                                          and UNAVAILABLE_MARKER.search(text))):
                v("qa_policy", "traffic data is unavailable: say that traffic cannot be assessed")
            elif TRAFFIC_FACT in {c.id for c in fs.categorical_facts} and TRAFFIC_FACT not in cited:
                v("qa_policy", f"the traffic statement must cite {TRAFFIC_FACT}")
        if ev.factors and not EVIDENCE_WORDING.search(text):
            v("qa_policy", "phrase the factors as 'evidence suggests …' contributing factors")
        m = DEFINITIVE.search(text)
        if m and not ev.single_cause_allowed:
            v("qa_policy", f"'{m.group(0)}': no single definitive cause (several factors, or data quality not high)")
    return ValidationResult(not violations, violations)
