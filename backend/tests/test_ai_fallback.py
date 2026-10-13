"""Phase 7C — the template report always passes the 7B validator (property test first), its
snapshots for the five fixtures, direction flipping, and word limits."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings

from app.ai.factsheet import factsheet_from_inputs, factsheet_hash
from app.ai.fallback import build_template_report
from app.ai.schemas import FactSheet
from app.ai.validator import MAX_BULLET_WORDS, MAX_REPORT_WORDS, validate_report
from tests.ai_strategies import factsheets

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
NAMES = sorted(p.name.removesuffix(".inputs.json") for p in FIXTURES.glob("*.inputs.json"))


def fixture(name: str) -> FactSheet:
    return factsheet_from_inputs(json.loads((FIXTURES / f"{name}.inputs.json").read_text(encoding="utf-8")))


def problems(fs: FactSheet) -> list[str]:
    report = build_template_report(fs)
    return [f"{v.where}: {v.rule} — {v.detail}" for v in validate_report(report, fs).violations]


# ---------------------------------------------------------------- the property (written first)
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(factsheets())
def test_template_report_always_passes_the_validator(fs):
    report = build_template_report(fs)
    result = validate_report(report, fs)
    assert result.ok, [f"{v.where}: {v.rule} — {v.detail} | {report.model_dump()}" for v in result.violations][:3]
    words = [len(b.text.split()) for b in [report.headline, *report.biggest_time_loss, *report.strongest_areas,
                                           *report.tire_notes, *report.driving_suggestions, *report.strategy_options,
                                           *report.data_limitations]]
    assert max(words) <= MAX_BULLET_WORDS and sum(words) <= MAX_REPORT_WORDS
    assert report.generated_by == "template" and report.factsheet_hash == fs.factsheet_hash


@pytest.mark.parametrize("name", NAMES)
def test_fixture_reports_pass_the_validator(name):
    assert problems(fixture(name)) == []


@pytest.mark.parametrize("name", NAMES)
def test_fixture_report_snapshots(name):
    """Reviewed by a human once (7C gate). Regenerate deliberately: python -m scripts.print_template_reports --write"""
    expected = json.loads((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8"))
    assert build_template_report(fixture(name)).model_dump(mode="json") == expected


# ---------------------------------------------------------------- direction flips with the drivers
def swapped(fs: FactSheet) -> FactSheet:
    """The same comparison seen from the other driver: target ↔ reference, every difference negated."""
    from app.ai.numbers import display_forms

    negated = []
    for f in fs.facts:
        field = f.id.split(".", 1)[1] if "." in f.id else ""
        if f.value is not None and (field.endswith(("_loss_s", "_diff_m", "_diff_kph", "_diff_s", "_delta_s", "_gap_s"))
                                    or field in ("time_diff_s", "gear_min_diff", "drs_open_fraction_diff")):
            negated.append(f.model_copy(update={"value": -f.value, "display": display_forms(-f.value, f.unit)}))
        else:
            negated.append(f)
    d = fs.drivers
    seg = [s.model_copy(update={"time_loss_s": None if s.time_loss_s is None else -s.time_loss_s}) for s in fs.segments]
    out = fs.model_copy(update={
        "facts": negated, "segments": seg,
        "drivers": d.model_copy(update={"target": d.reference, "reference": d.target}),
        "rankings": fs.rankings.model_copy(update={"largest_losses": fs.rankings.largest_gains,
                                                   "largest_gains": fs.rankings.largest_losses}),
        "lap_summary": fs.lap_summary.model_copy(update={
            "total_delta_s": None if fs.lap_summary.total_delta_s is None else -fs.lap_summary.total_delta_s}),
    })
    return out.model_copy(update={"factsheet_hash": factsheet_hash(out)})


def test_swapping_target_and_reference_flips_the_wording():
    fs = fixture("race_lec_pit_window")
    a, b = build_template_report(fs), build_template_report(swapped(fs))
    assert a.headline.text.startswith("LEC was 1.094 s slower than PIA over the lap and lost the most in Sector 2")
    assert b.headline.text.startswith("PIA was 1.094 s faster than LEC over the lap and gained the most in Sector 2")
    assert validate_report(b, swapped(fs)).ok
    # Turn 19 was LEC's biggest loss, so it is PIA's biggest gain once the roles swap
    assert "Turn 19" in a.biggest_time_loss[0].text and "Turn 19" in b.strongest_areas[0].text


# ---------------------------------------------------------------- budgets and edge cases
@pytest.mark.parametrize("budget", [450, 200, 120, 60])
def test_word_budget_is_respected_and_the_report_stays_valid(budget):
    fs = fixture("race_lec_pit_window")
    report = build_template_report(fs, max_words=budget)
    total = sum(len(b.text.split()) for b in [report.headline, *report.biggest_time_loss, *report.strongest_areas,
                                             *report.tire_notes, *report.driving_suggestions, *report.strategy_options,
                                             *report.data_limitations])
    assert total <= budget and report.headline.text
    assert validate_report(report, fs).ok


def test_no_losses_says_so_without_inventing_numbers():
    fs = fixture("race_lec_pit_window")
    no_losses = fs.model_copy(update={"rankings": fs.rankings.model_copy(update={"largest_losses": []})})
    no_losses = no_losses.model_copy(update={"factsheet_hash": factsheet_hash(no_losses)})
    report = build_template_report(no_losses)
    assert len(report.biggest_time_loss) == 1 and "noise floor" in report.biggest_time_loss[0].text
    assert validate_report(report, no_losses).ok


def test_tyre_unavailable_and_qualifying():
    q = build_template_report(fixture("q_alo_vs_ver_fastest"))
    assert q.strategy_options == [] and "unavailable" in q.tire_notes[0].text
    r = build_template_report(fixture("race_alo_tire_model_unavailable"))
    assert "503" in r.tire_notes[0].text  # the reason is quoted verbatim (and masked by the validator)
