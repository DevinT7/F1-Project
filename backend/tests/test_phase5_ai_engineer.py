"""
Phase 5 (Person 3): AI race engineer hardening, all offline, no API key needed.

Covers the Phase 5 definition-of-done items from P3_TIRE_AI_PLAN.md:
  - The grounding guardrail is *proven*: a stubbed LLM that returns a fabricated
    number is rejected and the template is served instead (source="template-fallback").
    A faithful rephrasing of the same facts is accepted.
  - All six of the project doc's example follow-up questions get a relevant,
    grounded answer on the template path.
  - P2's `warnings` reach the narrative, so "driver, tires, or traffic?" is
    answered from a real signal instead of being hand-waved.
  - The endpoint is async and still works with no key set.

    cd backend && pytest -q tests/test_phase5_ai_engineer.py
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.schemas import (
    AIInsight, AIReport, AIReportRequest, CornerInsight, DeltaPoint, Driver,
    LapComparison, LapPacePrediction, SectorDelta, TireAnalysis,
)
from app.services import ai_engineer

SESSION_KEY = 9159


def _comparison(warnings: list[str] | None = None) -> LapComparison:
    return LapComparison(
        session_key=SESSION_KEY,
        driver=Driver(driver_number=55, name_acronym="SAI"),
        reference=Driver(driver_number=1, name_acronym="VER"),
        lap_time=91.234, reference_lap_time=91.021,
        total_delta_s=0.213, time_gained_s=-0.087, time_lost_s=0.300,
        sector_deltas=[SectorDelta(sector=1, delta_s=0.050),
                       SectorDelta(sector=2, delta_s=0.180),
                       SectorDelta(sector=3, delta_s=-0.017)],
        delta_trace=[DeltaPoint(distance=0.0, delta_s=0.0),
                     DeltaPoint(distance=500.0, delta_s=0.213)],
        corners=[
            CornerInsight(corner_number=7, corner_name=None, distance=1234.0,
                          entry_speed=210.0, min_speed=140.0, exit_speed=230.0,
                          braking_point_m=1200.0, braking_delta_m=-18.0,
                          throttle_delay_s=0.4, min_speed_delta_kmh=-5.0,
                          time_delta_s=0.21),
            CornerInsight(corner_number=3, corner_name=None, distance=600.0,
                          entry_speed=250.0, min_speed=180.0, exit_speed=260.0,
                          braking_point_m=560.0, braking_delta_m=6.0,
                          throttle_delay_s=-0.1, min_speed_delta_kmh=4.0,
                          time_delta_s=-0.09),
        ],
        warnings=warnings or [],
    )


def _tires() -> TireAnalysis:
    return TireAnalysis(
        session_key=SESSION_KEY, driver_number=55, compound="MEDIUM",
        tyre_age_laps=14, degradation_s_per_lap=0.082,
        remaining_competitive_laps=9, recommended_pit_window=(24, 27),
        model_confidence=0.71,
        predictions=[LapPacePrediction(laps_ahead=1, predicted_lap_time_s=92.1,
                                       lower_s=91.8, upper_s=92.4),
                     LapPacePrediction(laps_ahead=5, predicted_lap_time_s=92.5,
                                       lower_s=91.9, upper_s=93.1)],
        model_name="baseline-linear",
    )


def _report(req: AIReportRequest) -> AIReport:
    return asyncio.run(ai_engineer.build_report(req))


def _text(report: AIReport) -> str:
    return " ".join([report.summary, *report.recommendations, *report.strategy_options,
                     *(i.label for i in report.insights)])


# ---------------------------------------------------------------- grounding guardrail

def _stub_llm(monkeypatch, report: AIReport):
    """Pretend a key is configured and the LLM returned `report`."""
    monkeypatch.setattr(ai_engineer.settings, "openai_api_key", "sk-test", raising=False)

    async def fake(_req, _facts):
        return report

    monkeypatch.setattr(ai_engineer, "_llm_report", fake)


def test_fabricated_number_is_rejected_and_falls_back_to_template(monkeypatch, caplog):
    """The guardrail the project doc asks for: a number that isn't in the facts
    means the entire LLM response is discarded, not patched up."""
    _stub_llm(monkeypatch, AIReport(
        summary="SAI was 0.213s slower than VER, and lost 1.87s in the final sector.",
        insights=[], recommendations=[], strategy_options=[],
        source="llm", grounded=True,
    ))
    report = _report(AIReportRequest(comparison=_comparison()))
    assert report.source == "template-fallback"
    assert "1.87" not in _text(report)
    assert report.grounded is True


def test_fabricated_number_in_an_insight_field_is_rejected(monkeypatch):
    """Not just prose — the numeric `delta_s` field is checked too."""
    _stub_llm(monkeypatch, AIReport(
        summary="Clean lap.", insights=[AIInsight(label="Sector 2", delta_s=0.999)],
        recommendations=[], strategy_options=[], source="llm", grounded=True,
    ))
    assert _report(AIReportRequest(comparison=_comparison())).source == "template-fallback"


def test_faithful_rephrasing_is_accepted(monkeypatch):
    """Rounding is fine: 0.21 for 0.213, 18 for 18.0, 71% for 0.71 confidence."""
    _stub_llm(monkeypatch, AIReport(
        summary="SAI lost 0.21s overall, 0.18s of it in sector 2. Braking 18m early "
                "into Turn 7 costs the most. Degradation 0.082s/lap, confidence 71%.",
        insights=[AIInsight(label="Sector 2", delta_s=0.18)],
        recommendations=["Pit in the lap 24-27 window."],
        strategy_options=["1-stop."], source="llm", grounded=True,
    ))
    report = _report(AIReportRequest(comparison=_comparison(), tires=_tires()))
    assert report.source == "llm"
    assert report.grounded is True


def test_llm_failure_is_logged_and_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(ai_engineer.settings, "openai_api_key", "sk-test", raising=False)

    async def boom(_req, _facts):
        raise TimeoutError("upstream took too long")

    monkeypatch.setattr(ai_engineer, "_llm_report", boom)
    with caplog.at_level("WARNING"):
        report = _report(AIReportRequest(tires=_tires()))
    assert report.source == "template-fallback"
    assert "TimeoutError" in caplog.text, "a timeout must be distinguishable in the logs"


def test_ungrounded_numbers_tolerance_scales_with_written_precision():
    facts = {"delta": 0.2134, "braking_m": 17.6}
    ok = AIReport(summary="0.21s and 18m", insights=[], recommendations=[],
                  strategy_options=[], source="llm")
    bad = AIReport(summary="0.2100s", insights=[], recommendations=[],
                   strategy_options=[], source="llm")
    assert ai_engineer._ungrounded_numbers(ok, facts) == []
    assert ai_engineer._ungrounded_numbers(bad, facts) == ["0.2100"]


# ---------------------------------------------------------------- follow-up questions

DOC_QUESTIONS = [
    "Where did I lose time compared to Verstappen?",
    "Why was I slower in sector 2?",
    "Is my tire degradation normal for this stint?",
    "Which corners should I focus on?",
    "When should I pit?",
    "Was the time loss caused by the driver, tires, or traffic?",
]


@pytest.mark.parametrize("question", DOC_QUESTIONS)
def test_every_doc_question_gets_a_real_answer(question):
    report = _report(AIReportRequest(comparison=_comparison(), tires=_tires(),
                                     question=question))
    assert "answered from the computed findings above" not in report.summary
    assert "won't guess" not in report.summary, f"no intent matched: {question}"
    # the answer has to carry a computed number, not just restate the question
    answer = report.summary.split(f"(Re: “{question}”)")[1]
    assert any(ch.isdigit() for ch in answer)


def test_unanswerable_question_says_so_instead_of_pretending():
    report = _report(AIReportRequest(comparison=_comparison(),
                                     question="What is the weather like in Monaco?"))
    assert "won't guess" in report.summary


def test_traffic_question_uses_p2_warnings():
    warning = ("SAI's lap 12 looks anomalous (much slower than their other laps this "
               "session) -- possible traffic, safety car, or red flag.")
    report = _report(AIReportRequest(
        comparison=_comparison(warnings=[warning]), tires=_tires(),
        question="Was the time loss caused by the driver, tires, or traffic?"))
    assert "traffic" in report.summary.lower()
    assert "lap 12" in report.summary
    assert "establishes which one caused" in report.summary, "must stay non-causal"


def test_cross_reference_is_hedged_not_causal():
    report = _report(AIReportRequest(comparison=_comparison(), tires=_tires()))
    hedge = [r for r in report.recommendations if "Worth checking" in r]
    assert hedge, "corner-exit vs degradation observation should be present"
    assert "does not show one causing the other" in hedge[0]


def test_predictions_and_remaining_life_reach_the_narrative():
    strategy = " ".join(_report(AIReportRequest(tires=_tires())).strategy_options)
    assert "9 competitive laps" in strategy
    assert "92.500" in strategy and "5 laps" in strategy


# ---------------------------------------------------------------- endpoint

def test_endpoint_is_async_and_works_with_no_key():
    r = TestClient(app).post("/api/ai/report", json={
        "tires": _tires().model_dump(), "question": "When should I pit?"})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template"
    assert body["grounded"] is True
    assert "24" in body["summary"] and "27" in body["summary"]


def test_tires_only_request_degrades_gracefully():
    report = _report(AIReportRequest(tires=_tires()))
    assert report.summary and report.strategy_options
    assert report.insights == []


def test_empty_request_says_nothing_rather_than_inventing():
    report = _report(AIReportRequest())
    assert "No findings supplied" in report.summary
    assert ai_engineer._ungrounded_numbers(report, {}) == []
