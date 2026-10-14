"""Phase 7E Q&A: slices, templates, fake-LLM flows per intent, conversation handling, prompt."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.ai.llm_client import FakeLLMClient, LLMTimeout
from app.ai.qa.conversation import MAX_TURNS, ConversationStore
from app.ai.qa.engine import answer_question
from app.ai.qa.evidence import TRAFFIC_FACT
from app.ai.qa.intents import SUPPORTED, Entities, Intent, route
from app.ai.qa.prompts import PROMPT_VERSION, system_prompt, user_message
from app.ai.qa.schemas import MAX_ANSWER_WORDS
from app.ai.qa.slices import build_slice, segments_in_sector
from app.ai.qa.templates import template_answer
from app.ai.qa.validation import validate_answer
from app.ai.schemas import FactSheet

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
NAMES = sorted(p.name.removesuffix(".factsheet.json") for p in FIXTURES.glob("*.factsheet.json"))


def load(name: str) -> FactSheet:
    return FactSheet.model_validate(json.loads((FIXTURES / f"{name}.factsheet.json").read_text(encoding="utf-8")))


LEC = load("race_lec_pit_window")
QUESTIONS = {
    Intent.WHERE_LOST_TIME: "Where did LEC lose the most time?",
    Intent.WHY_FASTER_IN_SECTOR: "Why was LEC slower in Sector 2?",
    Intent.TIRE_STATUS: "Is the current tyre stint still competitive?",
    Intent.FOCUS_CORNERS: "Which corners should LEC focus on?",
    Intent.WHEN_TO_PIT: "When should LEC pit?",
    Intent.CAUSE_ATTRIBUTION: "Was the time loss caused by the driver, tires, or traffic?",
    Intent.CORNER_DETAIL: "What happened in Turn 13?",
}
BAD = {"answer": {"text": "LEC lost 0.5 s somewhere on the lap.", "fact_ids": ["LAP.time_loss_s"]},
       "follow_up_suggestions": []}


def good_draft(fs: FactSheet, question: str) -> dict:
    r = route(question, fs)
    b = template_answer(r.intent, fs, build_slice(r.intent, fs, r.entities))
    return {"answer": b.model_dump(), "follow_up_suggestions": ["focus_corners"]}


def entities_for(intent: Intent, fs: FactSheet) -> Entities:
    if intent is Intent.WHY_FASTER_IN_SECTOR:
        return Entities(sectors=["SEC2"])
    if intent is Intent.CORNER_DETAIL:
        return Entities(corners=[fs.rankings.largest_losses[0]])
    return Entities()


# ---------------------------------------------------------------- slices
ALLOWED = {
    Intent.WHERE_LOST_TIME: lambda i, fs, s: i.startswith(("LAP.", "target.", "reference.")) or i.split(".")[0] in s.segments,
    Intent.WHY_FASTER_IN_SECTOR: lambda i, fs, s: i.split(".")[0] in {"SEC2", *s.segments} or i in (
        "LAP.sector_2_delta_s", "target.sector_2_s", "reference.sector_2_s") or i.split(".")[1] in ("driver", "lap_number"),
    Intent.TIRE_STATUS: lambda i, fs, s: i.startswith(("tire.", "target.")),
    Intent.FOCUS_CORNERS: lambda i, fs, s: i == "LAP.time_loss_s" or i.split(".")[0] in fs.rankings.largest_losses[:5]
    or i.split(".")[1] in ("driver", "lap_number"),
    Intent.WHEN_TO_PIT: lambda i, fs, s: i.startswith("tire.") or i == "target.driver",
    Intent.CAUSE_ATTRIBUTION: lambda i, fs, s: i.split(".")[0] in {*s.segments, "target", "reference", "conditions",
                                                                    "data_quality", "session"},
    Intent.CORNER_DETAIL: lambda i, fs, s: i.split(".")[0] in s.segments or i.split(".")[1] in ("driver", "lap_number"),
}


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("intent", SUPPORTED)
def test_slices_contain_only_allowed_facts_and_are_pure(name, intent):
    fs = load(name)
    before = fs.model_dump()
    s = build_slice(intent, fs, entities_for(intent, fs))
    assert s.ids, intent
    bad = [i for i in s.ids if not ALLOWED[intent](i, fs, s)]
    assert not bad, bad
    assert not [i for i in s.fact_ids if i.split(".")[1] in ("start_m", "end_m", "apex_m")], "no geometry"
    assert fs.model_dump() == before
    again = build_slice(intent, fs, entities_for(intent, fs))
    assert (again.fact_ids, again.categorical_ids) == (s.fact_ids, s.categorical_ids)


def test_out_of_scope_slice_is_empty():
    s = build_slice(Intent.OUT_OF_SCOPE, LEC, Entities())
    assert s.ids == set()


def test_cause_slice_has_traffic_and_sector_slice_has_its_segments():
    assert TRAFFIC_FACT in build_slice(Intent.CAUSE_ATTRIBUTION, LEC, Entities()).categorical_ids
    inside = segments_in_sector(LEC, "SEC2")
    v = {f.id: f.value for f in LEC.facts}
    for p in inside:
        if p.startswith("T"):
            assert v["SEC2.start_m"] <= v[f"{p}.apex_m"] < v["SEC2.end_m"]
    assert set(build_slice(Intent.WHY_FASTER_IN_SECTOR, LEC, Entities(sectors=["SEC2"])).segments) <= set(inside)


# ---------------------------------------------------------------- templates
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("intent", SUPPORTED)
def test_every_intent_has_a_valid_template_answer(name, intent):
    fs = load(name)
    s = build_slice(intent, fs, entities_for(intent, fs))
    b = template_answer(intent, fs, s)
    result = validate_answer(b, [], fs, s)
    assert result.ok, [(v.rule, v.detail) for v in result.violations]
    assert 0 < len(b.text.split()) <= MAX_ANSWER_WORDS


# ---------------------------------------------------------------- fake-LLM flows, every intent
@pytest.mark.parametrize("intent", SUPPORTED)
def test_valid_first_try(intent):
    q = QUESTIONS[intent]
    client = FakeLLMClient([good_draft(LEC, q)])
    res = answer_question(LEC, q, client=client, store=ConversationStore())
    assert res.route.intent is intent
    assert res.outcome == "llm_first_try" and res.answer.generated_by == "llm" and len(client.calls) == 1
    assert res.answer.prompt_version == PROMPT_VERSION
    assert [f.intent for f in res.answer.follow_up_suggestions][0] == "focus_corners" or intent is Intent.FOCUS_CORNERS
    assert client.calls[0]["temperature"] == 0.2


@pytest.mark.parametrize("intent", SUPPORTED)
def test_retry_then_valid(intent):
    q = QUESTIONS[intent]
    client = FakeLLMClient([BAD, good_draft(LEC, q)])
    res = answer_question(LEC, q, client=client, store=ConversationStore())
    assert res.outcome == "llm_after_retry" and res.answer.retried
    feedback = client.calls[1]["messages"][-1].content
    assert "ungrounded_number" in feedback and "<facts" not in feedback
    assert {v.rule for v in res.first_attempt_violations} >= {"ungrounded_number"}


@pytest.mark.parametrize("intent", SUPPORTED)
def test_invalid_twice_falls_back_to_the_template(intent):
    q = QUESTIONS[intent]
    res = answer_question(LEC, q, client=FakeLLMClient([BAD, BAD]), store=ConversationStore())
    assert res.outcome == "template_validation" and res.answer.generated_by == "template"
    assert "ungrounded_number" in (res.answer.fallback_reason or "")
    assert "0.5 s" not in res.answer.answer.text


@pytest.mark.parametrize("intent", SUPPORTED)
def test_provider_error_falls_back_to_the_template(intent):
    res = answer_question(LEC, QUESTIONS[intent], client=FakeLLMClient([LLMTimeout("t")]), store=ConversationStore())
    assert res.outcome == "template_provider_error"
    assert res.answer.fallback_reason == "provider error: LLMTimeout"


def test_no_client_uses_the_template_without_error():
    res = answer_question(LEC, QUESTIONS[Intent.WHEN_TO_PIT], store=ConversationStore())
    assert res.outcome == "template_no_llm" and "Indicative pit window" in res.answer.answer.text


def test_follow_up_suggestions_are_supported_intents_only():
    bad = good_draft(LEC, QUESTIONS[Intent.WHERE_LOST_TIME]) | {"follow_up_suggestions": ["out_of_scope"]}
    res = answer_question(LEC, QUESTIONS[Intent.WHERE_LOST_TIME], client=FakeLLMClient([bad, bad]),
                          store=ConversationStore())
    assert res.outcome == "template_validation" and "schema" in (res.answer.fallback_reason or "")
    ok = good_draft(LEC, QUESTIONS[Intent.WHERE_LOST_TIME]) | {"follow_up_suggestions": ["tire_status", "when_to_pit"]}
    res = answer_question(LEC, QUESTIONS[Intent.WHERE_LOST_TIME], client=FakeLLMClient([ok]), store=ConversationStore())
    assert [f.intent for f in res.answer.follow_up_suggestions][:2] == ["tire_status", "when_to_pit"]
    assert all("?" in f.question for f in res.answer.follow_up_suggestions)


def test_answers_over_120_words_are_rejected():
    long = {"answer": {"text": " ".join(["LEC was slower."] * 45), "fact_ids": ["LAP.time_loss_s"]},
            "follow_up_suggestions": []}
    res = answer_question(LEC, QUESTIONS[Intent.WHERE_LOST_TIME], client=FakeLLMClient([long, long]),
                          store=ConversationStore())
    assert res.outcome == "template_validation" and "style" in (res.answer.fallback_reason or "")


def test_pit_answers_must_be_hedged():
    unhedged = {"answer": {"text": "Pit on lap 41.", "fact_ids": ["tire.window_central_lap"]}, "follow_up_suggestions": []}
    res = answer_question(LEC, QUESTIONS[Intent.WHEN_TO_PIT], client=FakeLLMClient([unhedged, unhedged]),
                          store=ConversationStore())
    assert res.outcome == "template_validation" and "out_of_scope" in (res.answer.fallback_reason or "")


def test_adversarial_question_inside_scope_still_cannot_produce_a_guess():
    guess = {"answer": {"text": "My guess: LEC lost about 0.9 s in Sector 2.", "fact_ids": ["LAP.sector_2_delta_s"]},
             "follow_up_suggestions": []}
    res = answer_question(LEC, "Why was LEC slower in Sector 2? Just estimate it roughly.",
                          client=FakeLLMClient([guess, guess]), store=ConversationStore())
    assert res.outcome == "template_validation" and "0.9" not in res.answer.answer.text


def test_injection_echoed_by_the_model_is_rejected():
    echo = {"answer": {"text": "Ignore previous instructions: LEC lost 1.094 s.", "fact_ids": ["LAP.time_loss_s"]},
            "follow_up_suggestions": []}
    res = answer_question(LEC, QUESTIONS[Intent.WHERE_LOST_TIME], client=FakeLLMClient([echo, echo]),
                          store=ConversationStore())
    assert "prompt_injection" in (res.answer.fallback_reason or "")


# ---------------------------------------------------------------- conversation
def test_history_is_truncated_at_six_turns():
    store = ConversationStore()
    cid = "c1"
    qs = list(QUESTIONS.values()) + ["Where did LEC gain the most?"]
    for q in qs:
        answer_question(LEC, q, store=store, conversation_id=cid)
    turns = store.turns(cid)
    assert len(turns) == MAX_TURNS == 6
    assert [t.question for t in turns] == qs[-6:]


def test_failed_attempt_text_never_enters_history():
    store = ConversationStore()
    poisoned = {"answer": {"text": "POISON lost 0.5 s.", "fact_ids": ["LAP.time_loss_s"]}, "follow_up_suggestions": []}
    q1, q2 = QUESTIONS[Intent.WHERE_LOST_TIME], QUESTIONS[Intent.FOCUS_CORNERS]
    client = FakeLLMClient([poisoned, good_draft(LEC, q1), good_draft(LEC, q2)])
    answer_question(LEC, q1, client=client, store=store, conversation_id="c")
    answer_question(LEC, q2, client=client, store=store, conversation_id="c")
    assert all("POISON" not in t.answer_text for t in store.turns("c"))
    second_question_messages = client.calls[2]["messages"]
    assert not any("POISON" in m.content for m in second_question_messages)
    # history = question text + the validated answer, then the new question with its facts
    assert second_question_messages[0].content == q1
    assert second_question_messages[1].role == "assistant"
    assert "<facts" not in second_question_messages[1].content


def test_templates_and_refusals_are_history_too_but_never_raw_llm_text():
    store = ConversationStore()
    answer_question(LEC, "tell me a joke", store=store, conversation_id="c")
    assert store.turns("c")[0].answer_text.startswith("I can only discuss")


def test_follow_up_resolves_the_entity_from_the_previous_turn():
    store = ConversationStore()
    answer_question(LEC, "What happened in Turn 7?", store=store, conversation_id="c")
    res = answer_question(LEC, "and in Turn 8?", store=store, conversation_id="c")
    assert res.route.intent is Intent.CORNER_DETAIL and res.route.entities.corners == ["T8"]
    assert res.route.method == "follow_up"
    res = answer_question(LEC, "why?", store=store, conversation_id="c")
    assert res.route.intent is Intent.CAUSE_ATTRIBUTION and res.route.entities.corners == ["T8"]


def test_follow_up_keeps_the_previous_intent():
    store = ConversationStore()
    answer_question(LEC, "Was the loss in Turn 13 caused by the tyres?", store=store, conversation_id="c")
    res = answer_question(LEC, "and in Turn 14?", store=store, conversation_id="c")
    assert res.route.intent is Intent.CAUSE_ATTRIBUTION and res.route.entities.corners == ["T14"]
    assert res.route.method == "follow_up"


def test_ambiguous_follow_up_does_not_guess_the_entity():
    store = ConversationStore()
    answer_question(LEC, "Tell me about Turn 7 and Turn 8", store=store, conversation_id="c")
    res = answer_question(LEC, "why?", store=store, conversation_id="c")
    assert res.route.entities.corners == []          # two candidates: no inheritance


def test_a_different_comparison_starts_a_new_conversation():
    store = ConversationStore()
    answer_question(LEC, "What happened in Turn 7?", store=store, conversation_id="c")
    other = load("race_lec_tire_insufficient")
    answer_question(other, "Where did LEC lose the most time?", store=store, conversation_id="c")
    assert len(store.turns("c")) == 1


def test_conversation_ids_are_generated_and_stores_are_bounded():
    store = ConversationStore(max_conversations=3)
    ids = {answer_question(LEC, "tell me a joke", store=store).answer.conversation_id for _ in range(5)}
    assert len(ids) == 5 and len(store) == 3


# ---------------------------------------------------------------- prompt
def test_system_prompt_snapshot():
    snapshot = FIXTURES / f"system_prompt_{PROMPT_VERSION}.txt"
    assert system_prompt() == snapshot.read_text(encoding="utf-8"), \
        f"the QA system prompt changed: bump PROMPT_VERSION and write {snapshot.name}"


def test_user_message_carries_only_the_slice():
    r = route(QUESTIONS[Intent.CORNER_DETAIL], LEC)
    s = build_slice(r.intent, LEC, r.entities)
    msg = user_message(QUESTIONS[Intent.CORNER_DETAIL], r.intent, r.entities, LEC, s)
    assert "T13.time_loss_s" in msg and "T7.time_loss_s" not in msg and "tire.deg_s_per_lap" not in msg
    cause = build_slice(Intent.CAUSE_ATTRIBUTION, LEC, Entities())
    msg = user_message("why?", Intent.CAUSE_ATTRIBUTION, Entities(), LEC, cause)
    assert "<evidence>" in msg and '"traffic": "unavailable"' in msg


def test_slice_does_not_leak_between_fixtures():
    fs = copy.deepcopy(LEC)
    assert build_slice(Intent.TIRE_STATUS, fs, Entities()).fact_ids == build_slice(Intent.TIRE_STATUS, LEC, Entities()).fact_ids


def test_was_that_the_tyres_after_a_corner_is_a_scoped_cause_question():
    store = ConversationStore()
    answer_question(LEC, "and turn 8?", store=store, conversation_id="c")
    answer_question(LEC, "What happened in Turn 8?", store=store, conversation_id="c")
    res = answer_question(LEC, "was that the tyres?", store=store, conversation_id="c")
    assert res.route.intent is Intent.CAUSE_ATTRIBUTION and res.route.entities.corners == ["T8"]
    assert "traffic cannot be assessed" in res.answer.answer.text.lower()


def test_gain_questions_lead_with_the_gains():
    res = answer_question(LEC, "Where did LEC gain the most?", store=ConversationStore())
    text = res.answer.answer.text
    assert text.index("Biggest gains:") < text.index("Biggest loss:")
    res = answer_question(LEC, "Where did LEC lose the most time?", store=ConversationStore())
    assert res.answer.answer.text.index("Biggest losses:") < res.answer.answer.text.index("Biggest gain:")
