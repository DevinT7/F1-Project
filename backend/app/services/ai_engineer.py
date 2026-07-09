"""
AI Race Engineer (Person 3 → Person 4).

Turns the *computed* findings (LapComparison + TireAnalysis) into a natural-language
engineering report. The hard guardrail from the project doc: **the model never invents
numbers.** Every figure in the output is pulled from the structured findings; the LLM
only rephrases. If no API key is set, a deterministic template produces the same shape.
"""
from __future__ import annotations

import json

from ..config import settings
from ..models.schemas import AIInsight, AIReport, AIReportRequest


def build_report(req: AIReportRequest) -> AIReport:
    facts = _extract_facts(req)
    if settings.openai_api_key:
        try:
            return _llm_report(req, facts)
        except Exception:  # never fail the request on LLM issues — fall back
            pass
    return _template_report(req, facts)


def _extract_facts(req: AIReportRequest) -> dict:
    """Pull only verified, computed numbers. This is the grounding set."""
    facts: dict = {}
    c = req.comparison
    if c:
        facts["total_delta_s"] = c.total_delta_s
        facts["time_gained_s"] = c.time_gained_s
        facts["time_lost_s"] = c.time_lost_s
        facts["sectors"] = [{"sector": s.sector, "delta_s": s.delta_s}
                            for s in c.sector_deltas]
        # biggest loss / strongest gain by corner
        if c.corners:
            worst = max(c.corners, key=lambda x: x.time_delta_s)
            best = min(c.corners, key=lambda x: x.time_delta_s)
            facts["biggest_loss"] = {"corner": worst.corner_number, "delta_s": worst.time_delta_s}
            facts["strongest"] = {"corner": best.corner_number, "delta_s": best.time_delta_s}
        facts["driver"] = c.driver.name_acronym or str(c.driver.driver_number)
        facts["reference"] = c.reference.name_acronym or str(c.reference.driver_number)
    t = req.tires
    if t:
        facts["compound"] = t.compound
        facts["tyre_age_laps"] = t.tyre_age_laps
        facts["degradation_s_per_lap"] = t.degradation_s_per_lap
        facts["pit_window"] = t.recommended_pit_window
        facts["confidence"] = t.model_confidence
    return facts


def _template_report(req: AIReportRequest, facts: dict) -> AIReport:
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
        for s in c.sector_deltas:
            insights.append(AIInsight(label=f"Sector {s.sector}", delta_s=s.delta_s))
        if "biggest_loss" in facts and facts["biggest_loss"]["delta_s"] > 0:
            bl = facts["biggest_loss"]
            insights.append(AIInsight(label=f"Biggest loss · Turn {bl['corner']}",
                                      delta_s=bl["delta_s"]))
            recommendations.append(
                f"Focus on Turn {bl['corner']} — carry more minimum speed and get the "
                f"car rotated earlier to recover {bl['delta_s']:.3f}s.")
        if "strongest" in facts:
            st = facts["strongest"]
            insights.append(AIInsight(label=f"Strongest · Turn {st['corner']}",
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

    if req.question:
        summary_bits.append(f"(Re: “{req.question}”) — answered from the computed findings above.")

    if not summary_bits:
        summary_bits.append("No findings supplied. Run a lap comparison or tyre analysis first.")

    return AIReport(
        summary=" ".join(summary_bits),
        insights=insights,
        recommendations=recommendations or ["No specific weaknesses detected — clean lap."],
        strategy_options=strategy or ["Hold current strategy."],
        source="template", grounded=True,
    )


def _llm_report(req: AIReportRequest, facts: dict) -> AIReport:
    """Use an LLM to phrase the report, constrained to the verified facts only."""
    from openai import OpenAI  # lazy import

    client = OpenAI(api_key=settings.openai_api_key)
    system = (
        "You are an F1 race engineer. Write a concise, professional performance report. "
        "You are given VERIFIED numeric findings as JSON. Use ONLY those numbers — never "
        "invent or alter any statistic. Return strict JSON with keys: summary (string), "
        "insights (list of {label, delta_s}), recommendations (list of strings), "
        "strategy_options (list of strings)."
    )
    user = json.dumps({"facts": facts, "question": req.question}, default=str)
    resp = client.chat.completions.create(
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
