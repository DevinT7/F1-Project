"""Coverage gate (Phase 9): every NUMERICAL module must be ≥ 90% covered (line coverage).

    pytest --cov=app --cov-report=json:coverage.json -m "not network and not perf"
    python -m scripts.coverage_gate coverage.json

Numerical = where a wrong line silently produces wrong numbers: alignment, deltas, corner metrics,
findings, segmentation, display rounding and the validator, the FactSheet, normalisation, the tyre
features, models, intervals, pit window, degradation, evaluation and serving. Report generators
and I/O glue (tires/report.py, recovery.py, training_job.py, …) are not gated here.
"""
from __future__ import annotations

import json
import sys

THRESHOLD = 90.0
NUMERICAL = (
    "app/processing/alignment.py", "app/processing/delta.py", "app/processing/corner_metrics.py",
    "app/processing/insights.py", "app/processing/segments.py", "app/processing/models.py",
    "app/ai/numbers.py", "app/ai/validator.py", "app/ai/factsheet.py", "app/ingestion/normalize.py",
    "app/tires/features.py", "app/tires/degradation.py", "app/tires/evaluation.py", "app/tires/exclusions.py",
    "app/tires/pit_window.py", "app/tires/serving.py", "app/tires/bar.py", "app/tires/drift.py",
    "app/tires/models/baseline_fixed.py", "app/tires/models/baseline_stint.py", "app/tires/models/forecasting.py",
    "app/tires/models/intervals.py", "app/tires/models/persistence.py", "app/tires/models/tree_models.py",
)


def main(argv: list[str]) -> int:
    path = argv[0] if argv else "coverage.json"
    with open(path, encoding="utf-8") as fh:
        files = {k.replace("\\", "/"): v for k, v in json.load(fh)["files"].items()}
    failed = []
    for module in NUMERICAL:
        entry = next((v for k, v in files.items() if k.endswith(module)), None)
        if entry is None:
            failed.append(f"{module}: not measured")
            continue
        pct = entry["summary"]["percent_covered"]
        print(f"{'ok ' if pct >= THRESHOLD else 'LOW'} {pct:6.1f}%  {module}")
        if pct < THRESHOLD:
            failed.append(f"{module}: {pct:.1f}% < {THRESHOLD:.0f}%")
    if failed:
        print("\ncoverage gate FAILED:")
        for f in failed:
            print(f"  {f}")
        return 1
    print(f"\ncoverage gate passed: {len(NUMERICAL)} numerical modules >= {THRESHOLD:.0f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
