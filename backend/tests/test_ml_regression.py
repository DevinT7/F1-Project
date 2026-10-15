"""Phase 9 — the ML regression gate's decision logic (the full retrain runs weekly in CI)."""
from __future__ import annotations

import json

from scripts import ml_regression as mr


def _fake(monkeypatch, tmp_path, mae: float, sha: str | None = None):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"dataset_sha256": sha or mr.sha256(mr.DATASET),
                                    "metrics": {"pooled_fixed_effects_recent_mae_h5": 0.40}}), encoding="utf-8")
    monkeypatch.setattr(mr, "BASELINE", baseline)
    monkeypatch.setattr(mr, "evaluate", lambda frame: {"pooled_fixed_effects_recent_mae_h5": mae})


def test_within_tolerance_passes(monkeypatch, tmp_path):
    _fake(monkeypatch, tmp_path, 0.43)            # +7.5%
    assert mr.main([]) == 0


def test_more_than_ten_percent_worse_fails(monkeypatch, tmp_path):
    _fake(monkeypatch, tmp_path, 0.46)            # +15%
    assert mr.main([]) == 1


def test_a_changed_dataset_is_refused(monkeypatch, tmp_path):
    _fake(monkeypatch, tmp_path, 0.40, sha="0" * 64)
    assert mr.main([]) == 2


def test_the_recorded_baseline_matches_the_fixed_dataset():
    baseline = json.loads(mr.BASELINE.read_text(encoding="utf-8"))
    assert baseline["dataset_sha256"] == mr.sha256(mr.DATASET) and baseline["model"] == mr.MODEL
