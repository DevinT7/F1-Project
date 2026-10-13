"""The agreed 6D success bar (docs/tire_eval_baselines.md), applied mechanically.

During development the bar is checked RELATIVE to persistence on the same development races
(the absolute 0.332 s was set on all 24 races; 3 are now held out). The absolute numbers are
checked once, at the end, on the held-out races.

  1  LORO h=5 MAE ≤ 0.90 × persistence's
  2  |mean bias| ≤ 0.05 s (LORO, h=5)
  3  walk-forward h=5 MAE ≤ persistence's
  4  lower LORO h=5 MAE than persistence in a majority of races (paired)
  5  95% interval coverage within 90–97%; 80% intervals within 72–88% (only models with intervals)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

MAE_IMPROVEMENT = 0.10
BIAS_LIMIT_S = 0.05
COVERAGE_RANGE = (0.90, 0.97)
COVERAGE_80_RANGE = (0.72, 0.88)
REFERENCE = "persistence"


def coverage_range(model: str) -> tuple[float, float]:
    """Nominal level from the name suffix ("+conformal80", "+quantile80"); 95% otherwise."""
    return COVERAGE_80_RANGE if model.rsplit("+", 1)[-1].endswith("80") else COVERAGE_RANGE


@dataclass(frozen=True)
class Verdict:
    model: str
    loro_mae: float
    loro_target: float
    bias: float
    wf_mae: float | None
    wf_target: float | None
    races_won: int
    races_total: int
    coverage: float | None
    checks: dict[str, bool | None]  # None = not applicable

    @property
    def passes(self) -> bool:
        return all(v is not False for v in self.checks.values())


def _row(summary: pd.DataFrame, model: str, split: str, h: int) -> "pd.Series[Any] | None":
    rows = summary[(summary["model"] == model) & (summary["split"] == split) & (summary["horizon"] == h)]
    return rows.iloc[0] if len(rows) else None


def check(summary: pd.DataFrame, by_race: pd.DataFrame, horizon: int = 5) -> list[Verdict]:
    ref_loro = _row(summary, REFERENCE, "loro", horizon)
    if ref_loro is None:
        raise ValueError("the persistence benchmark must be in the evaluation")
    ref_wf = _row(summary, REFERENCE, "walk_forward", horizon)
    races = by_race[(by_race["split"] == "loro") & (by_race["horizon"] == horizon)]
    ref_races = races[races["model"] == REFERENCE].set_index("session_key")["mae"]
    verdicts = []
    for model in sorted(set(summary["model"]) - {REFERENCE}):
        loro = _row(summary, model, "loro", horizon)
        if loro is None:
            continue
        wf = _row(summary, model, "walk_forward", horizon)
        mine = races[races["model"] == model].set_index("session_key")["mae"]
        common = mine.index.intersection(ref_races.index)
        won = int((mine[common] < ref_races[common]).sum())
        target = (1 - MAE_IMPROVEMENT) * float(ref_loro["mae"])
        cov = float(loro["coverage"]) if pd.notna(loro.get("coverage")) else None
        checks: dict[str, bool | None] = {
            "1_mae_10pct_below_persistence": float(loro["mae"]) <= target,
            "2_bias_within_0.05": abs(float(loro["bias"])) <= BIAS_LIMIT_S,
            "3_walk_forward_not_worse": (float(wf["mae"]) <= float(ref_wf["mae"])) if wf is not None and ref_wf is not None else None,
            "4_wins_majority_of_races": won > len(common) / 2,
            "5_interval_coverage_90_97": (coverage_range(model)[0] <= cov <= coverage_range(model)[1]) if cov is not None else None,
        }
        verdicts.append(Verdict(model, float(loro["mae"]), target, float(loro["bias"]),
                                float(wf["mae"]) if wf is not None else None,
                                float(ref_wf["mae"]) if ref_wf is not None else None,
                                won, len(common), cov, checks))
    return verdicts


# ---------------------------------------------------------------- the final, absolute check
HOLDOUT_MAE_S = 0.332          # ≥ 10% below persistence's 0.369 s (6C, LORO h=5, all 24 races)
HOLDOUT_WF_REFERENCE_S = 0.390  # persistence walk-forward h=5 (6C)


def check_holdout(summary: pd.DataFrame, by_race: pd.DataFrame, horizon: int = 5) -> list[Verdict]:
    """The bar with its ABSOLUTE numbers, on the held-out races (split 'holdout'). Interval coverage
    is judged by nominal level: 80% intervals within 72–88%, 95% intervals within 90–97%."""
    races = by_race[(by_race["split"] == "holdout") & (by_race["horizon"] == horizon)]
    ref_races = races[races["model"] == REFERENCE].set_index("session_key")["mae"]
    verdicts = []
    for model in sorted(set(summary["model"]) - {REFERENCE}):
        row = _row(summary, model, "holdout", horizon)
        if row is None:
            continue
        mine = races[races["model"] == model].set_index("session_key")["mae"]
        common = mine.index.intersection(ref_races.index)
        won = int((mine[common] < ref_races[common]).sum())
        cov = float(row["coverage"]) if pd.notna(row.get("coverage")) else None
        lo, hi = coverage_range(model)
        checks: dict[str, bool | None] = {
            "1_mae_10pct_below_persistence": float(row["mae"]) <= HOLDOUT_MAE_S,
            "2_bias_within_0.05": abs(float(row["bias"])) <= BIAS_LIMIT_S,
            "3_walk_forward_not_worse": float(row["mae"]) <= HOLDOUT_WF_REFERENCE_S,
            "4_wins_majority_of_races": won > len(common) / 2,
            "5_interval_coverage_90_97": (lo <= cov <= hi) if cov is not None else None,
        }
        verdicts.append(Verdict(model, float(row["mae"]), HOLDOUT_MAE_S, float(row["bias"]), float(row["mae"]),
                                HOLDOUT_WF_REFERENCE_S, won, len(common), cov, checks))
    return verdicts
