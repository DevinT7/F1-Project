"""
AI Race Engineer (Person 3 → Person 4).

Turns the *computed* findings (LapComparison + TireAnalysis) into a natural-language
engineering report. The hard guardrail from the project doc: **the model never invents
numbers.** Every figure in the output is pulled from the structured findings; the LLM
only rephrases. If no API key is set, a deterministic template produces the same shape.

Phase 5 makes that guardrail real rather than asserted:

* `_extract_facts` is the single grounding set. `_ungrounded_numbers` re-reads every
  numeric token the LLM wrote back and checks it against that set within the rounding
  tolerance implied by how the number was written ("0.21" may be 0.213, "18" may be
  17.6). One unverifiable number and the whole LLM response is discarded — we serve the
  template and say so via `source="template-fallback"`.
* The LLM call is async with a timeout, so `POST /api/ai/report` never blocks the loop.
* Failures are logged with their exception type: a bad key, a timeout and a rate limit
  are distinguishable in the logs instead of all looking like "no key configured".
* `req.question` is actually answered (`_answer_question`) from the facts, on both the
  template and LLM paths, instead of being echoed back.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import settings
from ..models.schemas import AIInsight, AIReport, AIReportRequest

log = logging.getLogger(__name__)

# any number as it appears in prose: 18, 0.21, -1.5. The lookbehind stops a hyphen
# *range* ("laps 24-27") reading as the number -27, which would never be grounded.
_NUM_RE = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?")
# strategy prose counts stops ("1-stop", "2-stop"); those aren't findings, so they're
# the one thing allowed into the grounding set that didn't come from the facts.
_STRUCTURAL_NUMBERS = {1.0, 2.0}


async def build_report(req: AIReportRequest) -> AIReport:
    facts = _extract_facts(req)
    if not settings.openai_api_key:
        return _template_report(req, facts)
    try:
        report = await _llm_report(req, facts)
    except Exception as exc:  # never fail the request on LLM issues — fall back
        log.warning("AI report: %s from the LLM call (%s) — serving the template",
                    type(exc).__name__, exc)
    else:
        ungrounded = _ungrounded_numbers(report, facts)
        if not ungrounded:
            return report
        log.warning("AI report: discarding LLM output, %d number(s) are not in the "
                    "grounding set: %s", len(ungrounded), ungrounded)
    return _template_report(req, facts, source="template-fallback")


# ---------------------------------------------------------------- grounding set

def _extract_facts(req: AIReportRequest) -> dict:
    """Pull only verified, computed numbers. This is the grounding set: nothing
    outside this dict is allowed to appear as a number in the report."""
    facts: dict = {}
    c = req.comparison
    if c:
        facts["total_delta_s"] = c.total_delta_s
        facts["time_gained_s"] = c.time_gained_s
        facts["time_lost_s"] = c.time_lost_s
        facts["sectors"] = [{"sector": s.sector, "delta_s": s.delta_s}
                            for s in c.sector_deltas]
        # biggest loss / strongest gain by corner. Pull every grounded number P2
        # already computed for that corner (not just time_delta_s) so the
        # narrative can say *why* time was lost/gained, not just how much.
        if c.corners:
            worst = max(c.corners, key=lambda x: x.time_delta_s)
            best = min(c.corners, key=lambda x: x.time_delta_s)
            facts["biggest_loss"] = _corner_fact(worst)
            facts["strongest"] = _corner_fact(best)
        facts["driver"] = c.driver.name_acronym or str(c.driver.driver_number)
        facts["reference"] = c.reference.name_acronym or str(c.reference.driver_number)
        # P2 already decides whether either lap is traffic/SC-affected. Ignoring it
        # would make "driver, tires, or traffic?" unanswerable — see _a_attribution.
        if c.warnings:
            facts["warnings"] = list(c.warnings)
        facts["anomalous_lap"] = bool(c.warnings) or any(
            t is not None and t.is_anomalous_lap
            for t in (c.driver_trace, c.reference_trace))
    t = req.tires
    if t:
        facts["compound"] = t.compound
        facts["tyre_age_laps"] = t.tyre_age_laps
        facts["degradation_s_per_lap"] = t.degradation_s_per_lap
        facts["pit_window"] = t.recommended_pit_window   # RACE LAP NUMBERS (schemas.py)
        facts["confidence"] = t.model_confidence
        facts["remaining_competitive_laps"] = t.remaining_competitive_laps
        facts["predictions"] = [p.model_dump() for p in t.predictions]
        facts["model_name"] = t.model_name
    if req.question:
        facts["question"] = req.question
        answer = _answer_question(req.question, facts)
        if answer:
            facts["question_answer"] = answer
    return facts


def _corner_fact(c) -> dict:
    """Every grounded, already-computed number for one corner (see the
    consumer-field doc block on CornerInsight in schemas.py for sign
    conventions) -- the narrative below rephrases these, never recomputes or
    invents them."""
    return {
        "corner": c.corner_number,
        "corner_name": c.corner_name,
        "delta_s": c.time_delta_s,
        "braking_delta_m": c.braking_delta_m,
        "min_speed_delta_kmh": c.min_speed_delta_kmh,
        "throttle_delay_s": c.throttle_delay_s,
    }


def _allowed_numbers(node, out: set[float]) -> None:
    """Every number reachable in the facts — including numbers embedded in fact
    *strings* (P2's warnings quote lap numbers), so quoting a fact verbatim is
    always grounded."""
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float)):
        out.add(float(node))
    elif isinstance(node, str):
        out.update(float(m.group()) for m in _NUM_RE.finditer(node))
    elif isinstance(node, dict):
        for v in node.values():
            _allowed_numbers(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _allowed_numbers(v, out)


def _ungrounded_numbers(report: AIReport, facts: dict) -> list[str]:
    """Numbers the report states that the facts don't support. Empty list = the
    output is grounded and safe to serve.

    Tolerance comes from how the number was *written*: "0.21" is accepted against
    0.213 (±0.005), "18" against 17.6 (±0.5). That is exactly the rounding the
    template itself applies, so a faithful rephrasing always passes and an
    invented figure — which matches nothing at any precision — always fails.
    """
    allowed: set[float] = set()
    _allowed_numbers(facts, allowed)
    allowed |= {abs(v) for v in allowed}                          # "lost 0.21s" of -0.213
    allowed |= {v * 100 for v in allowed if 0.0 <= v <= 1.0}      # confidence as a %
    allowed |= _STRUCTURAL_NUMBERS

    literals = [m.group() for m in _NUM_RE.finditer(" ".join(
        [report.summary, *report.recommendations, *report.strategy_options,
         *(i.label for i in report.insights)]))]
    literals += [repr(i.delta_s) for i in report.insights if i.delta_s is not None]

    bad = []
    for lit in literals:
        value = float(lit)
        decimals = len(lit.partition(".")[2])
        tolerance = 0.5 * 10 ** -decimals + 1e-9
        if not any(abs(value - a) <= tolerance for a in allowed):
            bad.append(lit)
    return bad


# ---------------------------------------------------------------- narrative pieces

def _corner_finding_phrase(corner_fact: dict) -> str:
    """"lost 0.21s in Turn 7, braked 18m earlier, full throttle 0.4s later" --
    the project doc's example structured-finding sentence, built purely from
    already-computed numbers (see _corner_fact)."""
    label = corner_fact.get("corner_name") or f"Turn {corner_fact['corner']}"
    bits = [f"lost {abs(corner_fact['delta_s']):.2f}s in {label}"] \
        if corner_fact["delta_s"] > 0 else [f"gained {abs(corner_fact['delta_s']):.2f}s in {label}"]

    bd = corner_fact.get("braking_delta_m")
    if bd is not None and abs(bd) >= 0.5:
        bits.append(f"braked {abs(bd):.0f}m {'later' if bd > 0 else 'earlier'}")

    sd = corner_fact.get("min_speed_delta_kmh")
    if sd is not None and abs(sd) >= 0.5:
        bits.append(f"carried {abs(sd):.0f} km/h {'more' if sd > 0 else 'less'} min speed")

    td = corner_fact.get("throttle_delay_s")
    if td is not None and abs(td) >= 0.01:
        bits.append(f"full throttle {abs(td):.2f}s {'later' if td > 0 else 'earlier'}")

    return ", ".join(bits)


def _corner_label(corner_fact: dict) -> str:
    return corner_fact.get("corner_name") or f"Turn {corner_fact['corner']}"


# ---------------------------------------------------------------- follow-up questions
#
# The project doc's six example questions, each answered from the grounding set.
# Order matters: "was the time loss caused by the driver, tires, or traffic?"
# contains "lost"/"time", so attribution is matched before the generic
# where-did-I-lose-time answer.

def _a_attribution(facts: dict) -> str | None:
    bits: list[str] = []
    if facts.get("warnings"):
        bits.append("Traffic/safety-car check: " + " ".join(facts["warnings"]))
    elif facts.get("anomalous_lap") is False:
        bits.append("Neither lap is flagged as traffic- or safety-car-affected.")
    bl = facts.get("biggest_loss")
    if bl and bl["delta_s"] > 0:
        bits.append(f"Driver side: {_corner_finding_phrase(bl)}.")
    deg = facts.get("degradation_s_per_lap")
    if deg is not None:
        age = facts.get("tyre_age_laps")
        age_bit = f" at {age} laps old" if age is not None else ""
        bits.append(f"Tyre side: the {facts.get('compound') or 'current'} set is "
                    f"degrading ~{deg:.3f}s/lap{age_bit}.")
    if not bits:
        return None
    bits.append("These are the signals side by side — nothing in the pipeline "
                "establishes which one caused the loss.")
    return " ".join(bits)


def _a_sector(question: str, facts: dict) -> str | None:
    sectors = facts.get("sectors")
    if not sectors:
        return None
    m = re.search(r"sector\s*(\d)", question)
    picked = [s for s in sectors if m and s["sector"] == int(m.group(1))] or sectors
    bits = [", ".join(
        f"sector {s['sector']}: {abs(s['delta_s']):.3f}s "
        f"{'lost' if s['delta_s'] > 0 else 'gained'}" for s in picked) + "."]
    bl = facts.get("biggest_loss")
    if bl and bl["delta_s"] > 0:
        bits.append(f"The biggest single-corner loss on the lap was {_corner_label(bl)} "
                    f"({_corner_finding_phrase(bl)}) — corners aren't mapped to sectors "
                    f"in this pipeline, so treat that as lap-wide context.")
    return " ".join(bits)


def _a_pit(facts: dict) -> str | None:
    window = facts.get("pit_window")
    if not window:
        return None
    lo, hi = window
    bits = [f"Pit in the lap {lo}–{hi} window (race lap numbers)."]
    rem = facts.get("remaining_competitive_laps")
    if rem is not None:
        bits.append(f"That's about {rem} competitive laps from now.")
    conf = facts.get("confidence")
    if conf is not None:
        bits.append(f"Model confidence in the degradation fit: {conf:.2f}.")
    return " ".join(bits)


def _a_competitive(facts: dict) -> str | None:
    deg = facts.get("degradation_s_per_lap")
    if deg is None:
        return None
    age = facts.get("tyre_age_laps")
    bits = [f"The {facts.get('compound') or 'current'} set is at {age} laps old and "
            f"degrading ~{deg:.3f}s/lap."]
    rem = facts.get("remaining_competitive_laps")
    bits.append(f"Estimated {rem} competitive laps left." if rem is not None else
                "No competitive-life estimate — the fit doesn't show a clear drop-off yet.")
    preds = facts.get("predictions") or []
    if preds:
        p = preds[-1]
        bits.append(f"In {p['laps_ahead']} laps the model projects ~"
                    f"{p['predicted_lap_time_s']:.3f}s "
                    f"({p['lower_s']:.3f}–{p['upper_s']:.3f}).")
    return " ".join(bits)


def _a_corners(facts: dict) -> str | None:
    bl, st = facts.get("biggest_loss"), facts.get("strongest")
    if not bl:
        return None
    bits = [f"Start with {_corner_label(bl)}: {_corner_finding_phrase(bl)}."]
    if st and st["delta_s"] < 0:
        bits.append(f"{_corner_label(st)} is already a strength "
                    f"({_corner_finding_phrase(st)}) — leave it alone.")
    return " ".join(bits)


def _a_where_lost(facts: dict) -> str | None:
    if "total_delta_s" not in facts:
        return None
    total = facts["total_delta_s"]
    bits = [f"Net {abs(total):.3f}s {'down on' if total > 0 else 'up on'} "
            f"{facts.get('reference', 'the reference')} "
            f"({facts.get('time_lost_s', 0):.3f}s lost, "
            f"{abs(facts.get('time_gained_s', 0)):.3f}s gained)."]
    losing = [s for s in facts.get("sectors", []) if s["delta_s"] > 0]
    if losing:
        bits.append("Losses by sector: " + ", ".join(
            f"S{s['sector']} {s['delta_s']:.3f}s" for s in losing) + ".")
    bl = facts.get("biggest_loss")
    if bl and bl["delta_s"] > 0:
        bits.append(f"Worst corner: {_corner_finding_phrase(bl)}.")
    return " ".join(bits)


# (keywords, answer) in priority order — first match wins.
_INTENTS: list[tuple[tuple[str, ...], object]] = [
    (("caused", "cause", "traffic", "blame", "my fault"),
     lambda q, f: _a_attribution(f)),
    (("sector",), _a_sector),
    (("pit", "stop", "box"), lambda q, f: _a_pit(f)),
    (("degradation", "degrade", "competitive", "tyre life", "tire life", "stint",
      "how much longer", "normal"), lambda q, f: _a_competitive(f)),
    (("corner", "focus", "improve", "work on", "turn"), lambda q, f: _a_corners(f)),
    (("where", "lose", "lost", "slower", "slow", "time"), lambda q, f: _a_where_lost(f)),
]


def _answer_question(question: str, facts: dict) -> str | None:
    """Answer one of the doc's follow-ups from the grounding set. Returns None when
    no intent matches or the needed facts weren't supplied — the caller then says so
    honestly instead of pretending it answered."""
    q = question.lower()
    for keywords, answer in _INTENTS:
        if any(k in q for k in keywords):
            result = answer(q, facts)
            if result:
                return result
    return None


# ---------------------------------------------------------------- template path

def _template_report(req: AIReportRequest, facts: dict, source: str = "template") -> AIReport:
    insights: list[AIInsight] = []
    recommendations: list[str] = []
    strategy: list[str] = []
    summary_bits: list[str] = []

    c = req.comparison
    if c:
        who = facts.get("driver", "Driver")
        ref = facts.get("reference", "reference")
        verdict = "faster than" if c.total_delta_s < 0 else "slower than"
        summary_bits.append(
            f"{who} was {abs(c.total_delta_s):.3f}s {verdict} {ref} on this lap.")
        summary_bits.extend(c.warnings)   # P2's traffic/SC caveats, verbatim
        for s in c.sector_deltas:
            insights.append(AIInsight(label=f"Sector {s.sector}", delta_s=s.delta_s))
        if "biggest_loss" in facts and facts["biggest_loss"]["delta_s"] > 0:
            bl = facts["biggest_loss"]
            insights.append(AIInsight(label=f"Biggest loss · {_corner_label(bl)}",
                                      delta_s=bl["delta_s"]))
            recommendations.append(f"Focus on {_corner_label(bl)} — {_corner_finding_phrase(bl)}.")
        if "strongest" in facts:
            st = facts["strongest"]
            insights.append(AIInsight(label=f"Strongest · {_corner_label(st)}",
                                      delta_s=st["delta_s"]))

    t = req.tires
    if t:
        summary_bits.append(
            f"On {t.compound or 'current'} tyres ({t.tyre_age_laps} laps old), "
            f"degradation is ~{t.degradation_s_per_lap:.3f}s/lap.")
        if t.recommended_pit_window:
            lo, hi = t.recommended_pit_window
            strategy.append(f"1-stop: pit around laps {lo}–{hi}.")
            recommendations.append(
                f"Plan the stop for the lap {lo}–{hi} window before pace drops off.")
        strategy.append("2-stop: only if degradation accelerates or under a safety car.")
        if t.remaining_competitive_laps is not None:
            strategy.append(
                f"Roughly {t.remaining_competitive_laps} competitive laps left on this set.")
        if t.predictions:
            p = t.predictions[-1]
            strategy.append(
                f"In {p.laps_ahead} laps the projected pace is ~{p.predicted_lap_time_s:.3f}s "
                f"({p.lower_s:.3f}–{p.upper_s:.3f} band).")

    # The doc's example report connects corner-exit loss to rising degradation. We
    # can show the two together, but nothing in this pipeline establishes causation,
    # so it ships as an explicitly hedged observation, never as a finding.
    bl = facts.get("biggest_loss")
    if t and bl and (bl.get("throttle_delay_s") or 0) > 0.05 and t.degradation_s_per_lap > 0.05:
        recommendations.append(
            f"Worth checking: the {bl['throttle_delay_s']:.2f}s later full throttle at "
            f"{_corner_label(bl)} coincides with ~{t.degradation_s_per_lap:.3f}s/lap "
            f"degradation. The data shows them together; it does not show one causing "
            f"the other.")

    if req.question:
        answer = facts.get("question_answer")
        summary_bits.append(
            f"(Re: “{req.question}”) {answer}" if answer else
            f"(Re: “{req.question}”) — that isn't answerable from the computed "
            f"findings supplied, so I won't guess.")

    if not summary_bits:
        summary_bits.append("No findings supplied. Run a lap comparison or tyre analysis first.")

    return AIReport(
        summary=" ".join(summary_bits),
        insights=insights,
        recommendations=recommendations or ["No specific weaknesses detected — clean lap."],
        strategy_options=strategy or ["Hold current strategy."],
        source=source, grounded=True,
    )


# ---------------------------------------------------------------- LLM path

async def _llm_report(req: AIReportRequest, facts: dict) -> AIReport:
    """Use an LLM to phrase the report, constrained to the verified facts only.

    `grounded` is set from `_ungrounded_numbers` in `build_report`, not asserted here.
    """
    from openai import AsyncOpenAI  # lazy import

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.request_timeout_s)
    system = (
        "You are an F1 race engineer. Write a concise, professional performance report. "
        "You are given VERIFIED numeric findings as JSON. Use ONLY those numbers — never "
        "invent or alter any statistic; every number you write is machine-checked against "
        "the findings and the whole response is discarded if one doesn't match. "
        "If a question and question_answer are present, answer the question using "
        "question_answer's content. Return strict JSON with keys: summary (string), "
        "insights (list of {label, delta_s}), recommendations (list of strings), "
        "strategy_options (list of strings)."
    )
    user = json.dumps({"facts": facts, "question": req.question}, default=str)
    resp = await client.chat.completions.create(
        model=settings.ai_model,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.3,
    )
    data = json.loads(resp.choices[0].message.content)
    return AIReport(
        summary=data.get("summary", ""),
        insights=[AIInsight(**i) for i in data.get("insights", [])],
        recommendations=data.get("recommendations", []),
        strategy_options=data.get("strategy_options", []),
        source="llm", grounded=True,
    )
