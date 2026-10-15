"""Phase 7B — the validator against the hand-labelled corpus (tests/ai/corpus), the direction
lexicon, a grounding property test, the retry explanation and performance."""
from __future__ import annotations

import copy
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.ai.factsheet import factsheet_from_inputs
from app.ai.schemas import FactSheet
from app.ai.validator import RULES, explain_for_retry, lexicon, validate_report

HERE = Path(__file__).parent
CORPUS = HERE / "ai" / "corpus"
FIXTURES = HERE / "fixtures" / "ai"


@lru_cache(maxsize=None)
def factsheet(name: str) -> FactSheet:
    return factsheet_from_inputs(json.loads((FIXTURES / f"{name}.inputs.json").read_text(encoding="utf-8")))


def load(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (CORPUS / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def base_report(fs: FactSheet) -> dict[str, Any]:
    """A minimal valid report: a correct headline and empty sections."""
    loss = fs.fact("LAP.time_loss_s")
    word = "behind" if (loss.value or 0) > 0 else "ahead of"
    return {
        "headline": {"text": f"{fs.drivers.target.code} finished the lap {loss.display[0]} s {word} "
                             f"{fs.drivers.reference.code}.", "fact_ids": ["LAP.time_loss_s"]},
        "biggest_time_loss": [], "strongest_areas": [], "tire_notes": [], "driving_suggestions": [],
        "strategy_options": [], "data_limitations": [],
        "generated_by": "template", "prompt_version": "corpus", "factsheet_hash": fs.factsheet_hash,
    }


def build(case: dict[str, Any]) -> tuple[dict[str, Any], FactSheet]:
    fs = factsheet(case["factsheet"])
    if "report" in case:
        report = json.loads(json.dumps(case["report"]).replace("$HASH", fs.factsheet_hash))
        return report, fs
    report = base_report(fs)
    if "report_patch" in case:
        report.update(copy.deepcopy(case["report_patch"]))
    else:
        bullet = copy.deepcopy(case["bullet"])
        if case["section"] == "headline":
            report["headline"] = bullet
        else:
            report[case["section"]] = [bullet]
    return report, fs


GOOD, BAD = load("good.jsonl"), load("bad.jsonl")


def test_base_reports_are_valid():
    for name in {c["factsheet"] for c in GOOD + BAD}:
        fs = factsheet(name)
        assert validate_report(base_report(fs), fs).ok, name


@pytest.mark.parametrize("case", GOOD, ids=[c["id"] for c in GOOD])
def test_good_corpus_passes(case):
    report, fs = build(case)
    result = validate_report(report, fs)
    assert result.ok, [f"{v.where}: {v.rule} — {v.detail}" for v in result.violations]


@pytest.mark.parametrize("case", BAD, ids=[c["id"] for c in BAD])
def test_bad_corpus_fails_with_the_expected_rule(case):
    report, fs = build(case)
    result = validate_report(report, fs)
    assert not result.ok
    hits = [v for v in result.violations if v.rule == case["expect"]]
    assert hits, f"expected {case['expect']}, got {[(v.rule, v.detail) for v in result.violations]}"
    if "detail" in case:
        assert any(case["detail"] in v.detail for v in hits), [v.detail for v in hits]
    if "expect_bullet" in case:
        assert any(v.bullet_index == case["expect_bullet"] for v in hits), [(v.where, v.detail) for v in hits]


def test_corpus_size_and_rule_coverage():
    assert len(GOOD) + len(BAD) >= 60 and len(GOOD) >= 25 and len(BAD) >= 35
    expected = [c["expect"] for c in BAD]
    for rule in RULES:
        assert expected.count(rule) >= 2, f"rule {rule} needs at least two bad examples"
    assert len({c["id"] for c in GOOD + BAD}) == len(GOOD) + len(BAD)


def test_every_rule_has_a_good_example_exercising_it():
    """Each rule is exercised by at least one passing case (the 'good' side of the rule)."""
    exercised = {
        "schema": "g30", "unknown_fact_id": "g02", "ungrounded_number": "g03", "ungrounded_label": "g23",
        "unit_mismatch": "g29", "direction_mismatch": "g04", "unsupported_causal_claim": "g14",
        "unavailable_data": "g07", "out_of_scope": "g08", "style": "g26", "prompt_injection": "g24",
    }
    assert set(exercised) == set(RULES)
    ids = {c["id"] for c in GOOD}
    assert set(exercised.values()) <= ids


# ---------------------------------------------------------------- lexicon
def test_every_lexicon_phrase_maps_unambiguously():
    """Run the real (consuming) matcher on each entry's examples: the only directions that come out
    are the entry's own, so no example is read two ways."""
    from app.ai.validator import direction_matches

    lex = lexicon()
    patterns = [e.source for e in lex.entries]
    assert len(patterns) == len(set(patterns)), "duplicate lexicon pattern"
    for entry in lex.entries:
        assert entry.examples, entry.source
        for example in entry.examples:
            found = direction_matches(example)
            assert found, (example, entry.source)
            assert found[0][0] is entry, (example, found[0][0].source, entry.source)
            signs = {(e.family, e.sign) for e, _m in found}
            assert signs == {(entry.family, entry.sign)}, (example, signs)


def test_cause_groups_are_direction_specific_first():
    lex = lexicon()
    names = [g[0] for g in lex.cause_groups]
    generic = {"braking", "min_speed", "throttle", "exit", "straight"}
    assert all(names.index(n) > max(names.index(m) for m in names if m not in generic) for n in generic)


# ---------------------------------------------------------------- property: display strings ground themselves
def _fact_pool(name: str) -> list[Any]:
    fs = factsheet(name)
    return [f for f in fs.facts if f.value is not None and f.unit in {"s", "km/h", "m", "°C", "s/lap", "count", "rank"}]


POOL = _fact_pool("race_lec_pit_window")
UNIT_TEXT = {"s": "s", "km/h": "km/h", "m": "m", "°C": "°C", "s/lap": "s/lap", "count": "", "rank": ""}


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(POOL), st.integers(0, 2)), min_size=1, max_size=4, unique_by=lambda t: t[0].id))
def test_neutral_templates_built_from_display_strings_are_grounded(picks):
    fs = factsheet("race_lec_pit_window")
    parts = []
    for fact, k in picks:
        shown = fact.display[min(k, len(fact.display) - 1)]
        parts.append(f"value {shown} {UNIT_TEXT[fact.unit]}".rstrip())
    report = base_report(fs)
    report["data_limitations"] = [{"text": "Recorded: " + "; ".join(parts) + ".", "fact_ids": [f.id for f, _ in picks]}]
    result = validate_report(report, fs)
    numeric = {"ungrounded_number", "unit_mismatch", "unavailable_data"}
    assert not (result.rules() & numeric), [(v.rule, v.detail) for v in result.violations]


# ---------------------------------------------------------------- retry explanation and performance
def test_explain_for_retry_is_concise_and_does_not_leak_the_factsheet():
    case = next(c for c in BAD if c["id"] == "b07")
    report, fs = build(case)
    text = explain_for_retry(validate_report(report, fs).violations)
    assert "ungrounded_number" in text and "biggest_time_loss[0]" in text and "0.35" in text
    assert len(text) < 1500
    assert "0.311" not in text and "meaning" not in text  # no fact values or FactSheet content


@pytest.mark.perf
def test_full_report_validates_in_under_20_ms():
    case = next(c for c in GOOD if c["id"] == "g30")
    report, fs = build(case)
    validate_report(report, fs)  # warm the index and lexicon
    runs = []
    for _ in range(30):
        t = time.perf_counter()
        validate_report(report, fs)
        runs.append((time.perf_counter() - t) * 1000)
    runs.sort()
    assert runs[len(runs) // 2] < 20, runs


def test_rule_names_are_documented():
    doc = (HERE.parents[1] / "docs" / "validator_rules.md").read_text(encoding="utf-8")
    for rule in RULES:
        assert f"`{rule}`" in doc, rule
    assert re.search(r"kill rate", doc, re.IGNORECASE)


def test_mutation_kill_rate_is_at_least_98_percent():
    from scripts.validator_mutation_test import run

    mutants = run(seed=7, per_token=3)
    killed = sum(m.killed for m in mutants)
    assert len(mutants) >= 150
    survivors = [(m.case_id, m.kind, m.after) for m in mutants if not m.killed]
    assert killed / len(mutants) >= 0.98, survivors
    # the only accepted survivors are the documented equivalent mutants (docs/validator_rules.md)
    assert {s[0] for s in survivors} <= {"g19"}, survivors
