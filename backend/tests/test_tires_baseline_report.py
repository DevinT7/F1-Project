"""Phase 6B — the real-data runner: every race analysed, failures recorded, ordering judged with CIs."""
from __future__ import annotations

import pandas as pd

from app.tires.baseline_report import RaceResult, markdown, run
from app.tires.synthetic import aligned_plans, make_race


def test_runs_every_race_and_flags_the_collinear_one():
    frame = pd.concat([make_race(seed=1, session_key="2099_01_R"),
                       make_race(seed=2, session_key="2099_02_R", plans=aligned_plans(20, 57))], ignore_index=True)
    results = run(frame, {"2099_01_R": "Good GP", "2099_02_R": "Aligned GP"})
    assert [r.key for r in results] == ["2099_01_R", "2099_02_R"] and all(r.error is None for r in results)
    good, aligned = results
    assert good.b_flags == [] and good.ordering() == "expected"
    assert "confounded_fuel_age" in aligned.b_flags
    text = markdown(results, "abc123" * 6)
    assert "2/2 races" in text and "`confounded_fuel_age` in 1/2 races (2099_02_R)" in text


def test_a_failing_race_is_recorded_not_skipped():
    broken = make_race(seed=3, session_key="2099_03_R").drop(columns=["fuel_proxy"])
    [result] = run(broken, {})
    assert result.error is not None and "fuel_proxy" in result.error
    assert "ERROR" in markdown([result], "x" * 64)


def _race(**deg: tuple[float, float, float]) -> RaceResult:
    return RaceResult(key="k", event="e", laps=1, deg=dict(deg))


def test_ordering_is_judged_with_confidence_intervals():
    assert _race(SOFT=(0.12, 0.10, 0.14), HARD=(0.05, 0.04, 0.06)).ordering() == "expected"
    # reversed point estimates, overlapping CIs → the data cannot tell them apart
    assert _race(SOFT=(0.05, 0.00, 0.10), MEDIUM=(0.07, 0.04, 0.10)).ordering() == "inconclusive"
    # reversed and clearly separated → a genuine exception to explain
    assert _race(MEDIUM=(0.03, 0.02, 0.04), HARD=(0.09, 0.08, 0.10)).ordering() == "exception"
    assert _race(HARD=(0.05, 0.04, 0.06)).ordering() == "n/a (one compound)"
