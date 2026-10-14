"""Phase 7E cause-attribution policy: the answer is a phrasing of a fixed evidence structure.

  * evidence = {driver_factors, tire_factors, traffic: available|unavailable, conditions}
  * traffic unavailable → the answer says traffic cannot be assessed (validator rule qa_policy)
  * "evidence suggests" + listed contributing factors; never a single definitive cause unless
    exactly one factor exists and data quality is high
  * the answer may cite only the evidence's facts
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.llm_client import FakeLLMClient
from app.ai.qa.engine import answer_question
from app.ai.qa.evidence import Evidence, Factor, build_evidence
from app.ai.qa.intents import Entities, Intent
from app.ai.qa.slices import build_slice
from app.ai.qa.templates import template_answer
from app.ai.qa.validation import validate_answer
from app.ai.schemas import Bullet, FactSheet

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
NAMES = sorted(p.name.removesuffix(".factsheet.json") for p in FIXTURES.glob("*.factsheet.json"))
Q = "Was the time loss caused by the driver, tires, or traffic?"


def load(name: str) -> FactSheet:
    return FactSheet.model_validate(json.loads((FIXTURES / f"{name}.factsheet.json").read_text(encoding="utf-8")))


LEC = load("race_lec_pit_window")


def cause_slice(fs: FactSheet, corners: list[str] | None = None):
    return build_slice(Intent.CAUSE_ATTRIBUTION, fs, Entities(corners=corners or []))


@pytest.mark.parametrize("name", NAMES)
def test_cause_slice_includes_the_traffic_availability_fact(name):
    s = cause_slice(load(name))
    assert "conditions.traffic_data" in s.categorical_ids
    assert s.evidence is not None and s.evidence.traffic == "unavailable"


def test_evidence_structure_lists_only_findings_causes_and_real_tyre_differences():
    ev = build_evidence(LEC, None)
    assert set(ev.as_dict()) >= {"driver_factors", "tire_factors", "traffic", "conditions", "data_quality"}
    for f in ev.driver_factors:
        listed = LEC.categorical_facts[[c.id for c in LEC.categorical_facts].index(f"{f.segment}.causes")].value
        assert f.kind in listed                                    # only causes the findings list
        assert all(i in {x.id for x in LEC.facts} | {c.id for c in LEC.categorical_facts} for i in f.fact_ids)
    # LEC 14 laps old vs PIA 9 laps: a tyre-age factor; same compound: no compound factor
    kinds = {f.kind for f in ev.tire_factors}
    assert kinds == {"tyre_age"}
    assert ev.conditions == []                                     # green flag, dry
    assert ev.single_cause_allowed is False


def test_scoped_evidence_uses_only_the_named_segment():
    ev = build_evidence(LEC, ["T13"])
    assert {f.segment for f in ev.driver_factors} <= {"T13"}


@pytest.mark.parametrize("name", NAMES)
def test_template_cause_answer_follows_the_policy(name):
    fs = load(name)
    s = cause_slice(fs)
    b = template_answer(Intent.CAUSE_ATTRIBUTION, fs, s)
    text = b.text.lower()
    assert "traffic cannot be assessed" in text
    assert "evidence suggests" in text or not (s.evidence and (s.evidence.driver_factors or s.evidence.tire_factors))
    for word in ("the cause was", "was caused by", "solely", "entirely", "definitely"):
        assert word not in text
    assert "conditions.traffic_data" in b.fact_ids
    assert validate_answer(b, [], fs, s).ok, validate_answer(b, [], fs, s).violations


def rules(text: str, ids: list[str], fs: FactSheet = LEC, corners: list[str] | None = None) -> set[str]:
    s = cause_slice(fs, corners)
    return validate_answer(Bullet(text=text, fact_ids=ids), [], fs, s).rules()


def test_traffic_unavailable_must_be_stated():
    ok = "Evidence suggests tyre age may be a contributing factor: LEC's tyres were 14 laps old, PIA's 9. " \
         "Traffic cannot be assessed: traffic data is unavailable."
    ids = ["target.tyre_age_laps", "reference.tyre_age_laps", "conditions.traffic_data"]
    assert rules(ok, ids) == set()
    missing = "Evidence suggests tyre age may be a contributing factor: LEC's tyres were 14 laps old, PIA's 9."
    assert "qa_policy" in rules(missing, ids[:2])
    # mentioning traffic without saying it is unavailable is not enough
    assert "qa_policy" in rules(missing + " Traffic was not a factor.", ids)


def test_no_single_definitive_cause_when_several_factors_exist():
    text = ("The time loss was caused by tyre age: LEC's tyres were 14 laps old. "
            "Traffic cannot be assessed: traffic data is unavailable.")
    assert "qa_policy" in rules(text, ["target.tyre_age_laps", "conditions.traffic_data"])
    text2 = ("Evidence suggests the tyres were solely responsible. Traffic cannot be assessed: traffic data is unavailable.")
    assert "qa_policy" in rules(text2, ["target.tyre_age_laps", "conditions.traffic_data"])


def test_evidence_suggests_wording_is_required_when_factors_exist():
    text = "LEC's tyres were 14 laps old. Traffic cannot be assessed: traffic data is unavailable."
    assert "qa_policy" in rules(text, ["target.tyre_age_laps", "conditions.traffic_data"])


def test_answer_may_cite_only_evidence_facts():
    text = ("Evidence suggests tyre age may be a contributing factor: LEC's tyres were 14 laps old, PIA's 9. "
            "The next lap is forecast at 107.392 s. Traffic cannot be assessed: traffic data is unavailable.")
    ids = ["target.tyre_age_laps", "reference.tyre_age_laps", "tire.next_lap_pred_s", "conditions.traffic_data"]
    assert "qa_policy" in rules(text, ids)


def test_unlisted_causes_are_rejected():
    text = ("Evidence suggests the engine was a contributing factor in Turn 13. "
            "Traffic cannot be assessed: traffic data is unavailable.")
    assert rules(text, ["T13.time_loss_s", "conditions.traffic_data"], corners=["T13"]) & {"unsupported_causal_claim",
                                                                                         "qa_policy"}


def single_factor_evidence(quality: str) -> Evidence:
    return Evidence(scope=["T13"], driver_factors=[Factor("early_braking_loss", "T13", "Turn 13",
                                                          ("T13.time_loss_s", "T13.braking_start_diff_m", "T13.causes"),
                                                          "Turn 13: LEC braked 12.0 m earlier")],
                    tire_factors=[], traffic="unavailable", conditions=[], data_quality=quality)  # type: ignore[arg-type]


def test_single_definitive_cause_needs_exactly_one_factor_and_high_quality():
    assert single_factor_evidence("high").single_cause_allowed
    assert not single_factor_evidence("medium").single_cause_allowed
    assert not build_evidence(LEC, None).single_cause_allowed


def test_obedient_llm_claiming_a_definitive_cause_falls_back_to_the_template():
    definitive = {"answer": {"text": "The loss was caused by the driver alone. Traffic cannot be assessed: traffic "
                                     "data is unavailable.", "fact_ids": ["conditions.traffic_data"]},
                  "follow_up_suggestions": []}
    res = answer_question(LEC, Q, client=FakeLLMClient([definitive, definitive]))
    assert res.outcome == "template_validation"
    assert "qa_policy" in res.answer.fallback_reason
    assert "traffic cannot be assessed" in res.answer.answer.text.lower()
