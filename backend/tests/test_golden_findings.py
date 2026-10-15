"""Phase 9 golden-file regression: findings for five fixture comparisons must not change unless the
golden files are re-recorded on purpose (`python -m scripts.golden_findings --write`) and the diff
is reviewed in the pull request."""
from __future__ import annotations

import json

import pytest

from scripts.golden_findings import CASES, diff, findings, path_for


@pytest.mark.parametrize("ref, tgt", CASES, ids=[f"{r}-vs-{t}" for r, t in CASES])
def test_findings_match_the_reviewed_golden_file(ref, tgt):
    path = path_for(ref, tgt)
    assert path.exists(), f"missing {path.name}: run `python -m scripts.golden_findings --write` and review it"
    problems = diff(json.loads(path.read_text(encoding="utf-8")), findings(ref, tgt))
    assert not problems, (f"{len(problems)} finding(s) changed vs {path.name}; if intended, re-record with "
                          f"`python -m scripts.golden_findings --write` and review the diff:\n  " + "\n  ".join(problems[:20]))


def test_the_diff_reports_real_changes_and_ignores_float_noise():
    base = {"a": 1.0, "b": [1, 2], "c": {"d": "x"}}
    assert diff(base, {"a": 1.0 + 1e-9, "b": [1, 2], "c": {"d": "x"}}) == []
    assert diff(base, {"a": 1.01, "b": [1, 2], "c": {"d": "x"}}) == ["/a: 1.0 → 1.01"]
    assert diff(base, {"a": 1.0, "b": [1], "c": {"d": "y"}, "e": 1}) == [
        "/b: length 2 → 1", '/c/d: "x" → "y"', "/e: added = 1"]


def test_swapping_reference_and_target_flips_the_lap_delta():
    a = json.loads(path_for("VER", "ALO").read_text(encoding="utf-8"))
    b = json.loads(path_for("ALO", "VER").read_text(encoding="utf-8"))
    assert a["lap_summary"]["time_loss_s"] == pytest.approx(-b["lap_summary"]["time_loss_s"], abs=0.02)
