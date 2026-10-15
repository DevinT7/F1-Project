"""ML regression check (Phase 9): retrain the production tyre model on a FIXED dataset and fail if
its accuracy worsens by more than 10% relative to the recorded baseline.

    cd backend && python -m scripts.ml_regression            # check (CI: .github/workflows/ml-regression.yml, weekly)
    cd backend && python -m scripts.ml_regression --record   # record a new baseline (after a reviewed model change)

Dataset: tests/fixtures/tires/regression_dataset_6681d16a0b57.parquet (the 6A dataset the production
model was built on; its SHA-256 is pinned, so the check can only move when the CODE changes).
Split: races in date order; fit on the earliest 70%, simulate the latest 30% exactly as 6C/6D do
(forecast cuts every few laps, horizon 5). Metric: MAE over all horizon-5 steps, plus horizon 10
and the persistence model for context. Baseline: models/ml_regression_baseline.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.tires.evaluation import race_order, simulate_race
from app.tires.models.forecasting import PooledFixedEffects
from app.tires.models.persistence import PersistenceModel

BACKEND = Path(__file__).resolve().parents[1]
DATASET = BACKEND / "tests" / "fixtures" / "tires" / "regression_dataset_6681d16a0b57.parquet"
BASELINE = BACKEND.parent / "models" / "ml_regression_baseline.json"
TRAIN_FRACTION = 0.7
TOLERANCE = 0.10                      # fail when MAE > baseline × 1.10
MODEL = "pooled_fixed_effects_recent"
ANCHOR_LAPS = 3                       # the production model (6E): Baseline B anchored on the last 3 laps


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(frame: pd.DataFrame) -> dict[str, Any]:
    order = race_order(frame)
    n_train = int(round(len(order) * TRAIN_FRACTION))
    train_races, test_races = order[:n_train], order[n_train:]
    train = frame[frame["session_key"].astype(str).isin(train_races)]
    out: dict[str, Any] = {"train_races": len(train_races), "test_races": len(test_races)}
    for name, model in ((MODEL, PooledFixedEffects(anchor_laps=ANCHOR_LAPS)), ("persistence", PersistenceModel())):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(train)
            records = pd.concat([simulate_race(model, frame[frame["session_key"].astype(str) == race], (5, 10))
                                 for race in test_races], ignore_index=True)
        for h in (5, 10):
            r = records[records["horizon"] == h]
            out[f"{name}_mae_h{h}"] = round(float(np.mean(np.abs(r["pred"] - r["actual"]))), 5)
        out[f"{name}_n_h5"] = int((records["horizon"] == 5).sum())
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.ml_regression")
    parser.add_argument("--record", action="store_true", help="write the current numbers as the new baseline")
    args = parser.parse_args(argv)
    digest = sha256(DATASET)
    result = evaluate(pd.read_parquet(DATASET))
    key = f"{MODEL}_mae_h5"
    print(json.dumps(result, indent=1))
    if args.record:
        BASELINE.write_text(json.dumps({"dataset": DATASET.name, "dataset_sha256": digest, "model": MODEL,
                                        "anchor_laps": ANCHOR_LAPS, "train_fraction": TRAIN_FRACTION,
                                        "tolerance": TOLERANCE, "recorded_at": datetime.now(timezone.utc).isoformat(),
                                        "metrics": result}, indent=1) + "\n", encoding="utf-8")
        print(f"baseline recorded: {key} = {result[key]}")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if baseline["dataset_sha256"] != digest:
        print("the fixed dataset changed (SHA-256 differs from the baseline): re-record deliberately", file=sys.stderr)
        return 2
    ref = float(baseline["metrics"][key])
    now = float(result[key])
    change = (now - ref) / ref
    print(f"{key}: baseline {ref:.4f}, now {now:.4f} ({change:+.1%}; limit +{TOLERANCE:.0%})")
    if change > TOLERANCE:
        print(f"ML REGRESSION: MAE worsened by {change:.1%} (> {TOLERANCE:.0%})", file=sys.stderr)
        return 1
    print("no ML regression")
    return 0


if __name__ == "__main__":
    sys.exit(main())
