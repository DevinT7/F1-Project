"""Phase 6D runner helpers on a tiny synthetic evaluation (tables, interval choice, model specs)."""
from __future__ import annotations

import pandas as pd
import pytest

from app.tires.evaluation import evaluate
from app.tires.models.forecasting import PooledFixedEffects
from app.tires.models.intervals import SplitConformal
from app.tires.models.persistence import PersistenceModel
from app.tires.synthetic import make_race
from scripts import run_6d as R

FRAME = pd.concat([make_race(seed=40 + i, session_key=f"2023_{i + 1:02d}_R") for i in range(6)], ignore_index=True)


@pytest.fixture(scope="module")
def result():
    models = [PersistenceModel(), SplitConformal(PersistenceModel(), alpha=0.2),
              SplitConformal(PooledFixedEffects(), alpha=0.2), SplitConformal(PooledFixedEffects(), alpha=0.05)]
    return evaluate(FRAME, models, splits=("loro", "walk_forward"), horizons=(5,), min_train=3)


def test_tables_list_every_model(result):
    for text in (R.comparison_table(result), R.verdict_table(result), R.interval_table(result)):
        assert "pooled_fixed_effects+conformal80" in text
    assert "72%–88%" in R.interval_table(result) and "90%–97%" in R.interval_table(result)


def test_interval_choice_is_an_80pct_method_within_tolerance_or_none(result):
    chosen = R.choose_interval(result)
    s = result.summary
    if chosen != "none within tolerance":
        row = s[(s["model"] == chosen) & (s["split"] == "walk_forward") & (s["horizon"] == 5)].iloc[0]
        assert chosen.endswith("80") and 0.72 <= row["coverage"] <= 0.88


def test_holdout_table_and_specs():
    res = evaluate(FRAME, [PersistenceModel(), PooledFixedEffects()], splits=("holdout",), horizons=(5,))
    assert set(res.by_race["session_key"]) == {"2023_04_R", "2023_05_R", "2023_06_R"}
    assert "2023_06_R" in R.comparison_table(res)
    assert R.point_model("lightgbm+identity", {"num_leaves": 15}).name == "lightgbm+identity"
    with pytest.raises(SystemExit):
        R.point_model("lightgbm+monotone", {})
