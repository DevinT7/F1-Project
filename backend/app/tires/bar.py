"""The agreed 6D success bar (docs/tire_eval_baselines.md), applied mechanically.

During development the bar is checked RELATIVE to persistence on the same development races
(the absolute 0.332 s was set on all 24 races; 3 are now held out). The absolute numbers are
checked once, at the end, on the held-out races.

  1  LORO h=5 MAE ≤ 0.90 × persistence's
  2  |mean bias| ≤ 0.05 s (LORO, h=5)
  3  walk-forward h=5 MAE ≤ persistence's
  4  lower LORO h=5 MAE than persistence in a majority of races (paired)
  5  95% interval coverage within 90–97% (only for models that publish intervals)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

MAE_IMPROVEMENT = 0.10
BIAS_LIMIT_S = 0.05
COVERAGE_RANGE = (0.90, 0.97)
REFERENCE = "persistence"


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
            "5_interval_coverage_90_97": (COVERAGE_RANGE[0] <= cov <= COVERAGE_RANGE[1]) if cov is not None else None,
        }
        verdicts.append(Verdict(model, float(loro["mae"]), target, float(loro["bias"]),
                                float(wf["mae"]) if wf is not None else None,
                                float(ref_wf["mae"]) if ref_wf is not None else None,
                                won, len(common), cov, checks))
    return verdicts
