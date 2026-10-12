"""Phase 6C — evaluation harness: splits, the information boundary, metrics, aggregation, determinism.

Written before the harness. Races are synthetic (app.tires.synthetic) with distinct keys/rounds so
the split logic sees a realistic multi-race dataset.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.tires import evaluation as E
from app.tires.models.forecasting import PooledFixedEffects, StintLinearExtrapolation
from app.tires.models.persistence import PersistenceModel
from app.tires.synthetic import make_race


def races(n: int = 10, seed0: int = 100) -> pd.DataFrame:
    keys = [f"{2023 + i // 6}_{(i % 6) + 1:02d}_R" for i in range(n)]
    return pd.concat([make_race(seed=seed0 + i, session_key=k) for i, k in enumerate(keys)], ignore_index=True)


FRAME = races()
MODELS = [PersistenceModel, StintLinearExtrapolation, PooledFixedEffects]


# ---------------------------------------------------------------- splits
def test_loro_tests_every_race_exactly_once_and_never_trains_on_it():
    folds = list(E.split_generator(FRAME, "loro"))
    tested = [f.test_key for f in folds]
    assert sorted(tested) == sorted(FRAME["session_key"].unique()) and len(set(tested)) == len(tested)
    for f in folds:
        assert f.test_key not in f.train_keys
        assert set(f.train["session_key"]) == set(f.train_keys) and set(f.test["session_key"]) == {f.test_key}


def test_walk_forward_only_trains_on_earlier_races():
    order = E.race_order(FRAME)
    folds = list(E.split_generator(FRAME, "walk_forward", min_train=8))
    assert [f.test_key for f in folds] == order[8:]
    for f in folds:
        position = order.index(f.test_key)
        assert set(f.train_keys) == set(order[:position])  # every earlier race, and only those
        assert f.test_key not in f.train_keys


def test_race_order_is_by_year_then_round():
    assert E.race_order(FRAME)[:3] == ["2023_01_R", "2023_02_R", "2023_03_R"]
    assert E.race_order(FRAME)[6] == "2024_01_R"


def test_unknown_split_kind_rejected():
    with pytest.raises(ValueError):
        list(E.split_generator(FRAME, "random"))


# ---------------------------------------------------------------- the information boundary
def _long_stint(df: pd.DataFrame) -> pd.DataFrame:
    sizes = df.groupby(["driver", "stint"]).size()
    driver, stint = sizes[sizes >= 18].index[0]
    return df[(df["driver"] == driver) & (df["stint"] == stint)].sort_values("lap_number").reset_index(drop=True)


def test_forecast_inputs_mask_everything_unknowable():
    stint = _long_stint(FRAME[FRAME["session_key"] == "2023_01_R"])
    history, future = E.forecast_inputs(stint, k=8, h=5)
    assert len(history) == 8 and len(future) == 5
    assert future["lap_time_s"].isna().all() and future["prev_lap_time_s"].isna().all()
    # weather and position frozen at the last known lap; fuel/tyre age/lap number are deterministic
    assert (future["track_temp_c"] == history["track_temp_c"].iloc[-1]).all()
    assert (future["position"] == history["position"].iloc[-1]).all()
    assert future["fuel_proxy"].tolist() == stint["fuel_proxy"].iloc[8:13].tolist()


@pytest.mark.parametrize("model_cls", MODELS, ids=lambda m: m.__name__)
def test_mutating_laps_after_k_never_changes_predictions(model_cls):
    test_key = "2023_01_R"
    train = FRAME[FRAME["session_key"] != test_key]
    test = FRAME[FRAME["session_key"] == test_key]
    model = model_cls()
    model.fit(train)
    stint = _long_stint(test)
    k, h = 8, 5
    before = model.forecast(*E.forecast_inputs(stint, k, h)).mean

    mutated = stint.copy()
    after_k = mutated.index[k:]
    rng = np.random.default_rng(0)
    for col in ("lap_time_s", "prev_lap_time_s", "track_temp_c", "air_temp_c"):
        mutated.loc[after_k, col] = rng.normal(500, 50, size=len(after_k))
    for col in ("position", "laps_since_neutralization"):  # integer columns get integers
        mutated.loc[after_k, col] = rng.integers(0, 99, size=len(after_k))
    mutated.loc[after_k, "rainfall"] = True
    after = model.forecast(*E.forecast_inputs(mutated, k, h)).mean
    np.testing.assert_array_equal(before, after)

    # ...while the history does matter (so the test is not vacuous)
    poked = stint.copy()
    poked.loc[poked.index[:k], "lap_time_s"] += 1.0
    assert not np.allclose(model.forecast(*E.forecast_inputs(poked, k, h)).mean, before)


def test_simulate_race_respects_cut_points_and_horizons():
    test = FRAME[FRAME["session_key"] == "2023_02_R"]
    model = PersistenceModel()
    model.fit(FRAME)
    recs = E.simulate_race(model, test, horizons=(5, 10))
    assert set(recs["horizon"]) == {5, 10}
    for (driver, stint, h), g in recs.groupby(["driver", "stint", "horizon"]):
        n = int(test[(test["driver"] == driver) & (test["stint"] == stint)].shape[0])
        assert n >= 12
        cuts = sorted(g["k"].unique())
        assert cuts == list(range(6, n - h + 1, 2))
        assert set(g["step"]) == set(range(1, h + 1))
    assert recs["actual"].notna().all() and recs["pred"].notna().all()


# ---------------------------------------------------------------- metrics (known answers)
def test_point_metrics_hand_computed():
    pred, actual = np.array([1.0, 2.0, 4.0]), np.array([1.5, 2.0, 2.0])
    # errors (pred - actual): -0.5, 0, +2
    assert E.mae(pred, actual) == pytest.approx((0.5 + 0 + 2) / 3)
    assert E.rmse(pred, actual) == pytest.approx(math.sqrt((0.25 + 0 + 4) / 3))
    assert E.bias(pred, actual) == pytest.approx((-0.5 + 0 + 2) / 3)


def test_interval_metrics_worked_example():
    # Gneiting & Raftery interval score, alpha = 0.1 (90% interval):
    #   IS = (u - l) + (2/a)(l - y)·1[y < l] + (2/a)(y - u)·1[y > u]
    lo, hi, y = np.array([0.0, 0.0, 0.0]), np.array([2.0, 2.0, 2.0]), np.array([1.0, -1.0, 3.5])
    # inside: 2 ; below by 1: 2 + 20·1 = 22 ; above by 1.5: 2 + 20·1.5 = 32  → mean 56/3
    assert E.interval_score(lo, hi, y, alpha=0.1) == pytest.approx(56 / 3)
    assert E.coverage(lo, hi, y) == pytest.approx(1 / 3)
    assert E.mean_width(lo, hi) == pytest.approx(2.0)


def test_mean_over_races_not_pooled_laps():
    # race A: 2 laps with error 1 ; race B: 98 laps with error 0
    recs = pd.DataFrame({"session_key": ["A"] * 2 + ["B"] * 98,
                         "pred": [1.0] * 2 + [0.0] * 98, "actual": [0.0] * 100})
    pooled = E.mae(recs["pred"].to_numpy(), recs["actual"].to_numpy())
    agg = E.aggregate(recs, E.mae)
    assert pooled == pytest.approx(0.02)
    assert agg.mean == pytest.approx(0.5)                      # races weigh equally
    assert agg.se == pytest.approx(np.std([1.0, 0.0], ddof=1) / math.sqrt(2))
    assert agg.per_race == {"A": 1.0, "B": 0.0}


def test_slope_reference_is_the_stints_own_ex_post_ols():
    stint = pd.DataFrame({"tyre_age": np.arange(1, 17), "lap_time_s": 90 + 0.1 * np.arange(1, 17)})
    assert E.ex_post_slope(stint) == pytest.approx(0.1)


# ---------------------------------------------------------------- end to end + determinism
def test_evaluate_runs_all_models_both_splits_and_is_deterministic():
    small = races(n=10)
    a = E.evaluate(small, [PersistenceModel(), StintLinearExtrapolation(), PooledFixedEffects()],
                   splits=("loro", "walk_forward"), horizons=(5,), min_train=8)
    b = E.evaluate(small, [PersistenceModel(), StintLinearExtrapolation(), PooledFixedEffects()],
                   splits=("loro", "walk_forward"), horizons=(5,), min_train=8)
    pd.testing.assert_frame_equal(a.summary, b.summary)
    assert set(a.summary["model"]) == {"persistence", "stint_linear_extrapolation", "pooled_fixed_effects"}
    assert set(a.summary["split"]) == {"loro", "walk_forward"}
    assert (a.summary["n_races"] > 0).all() and a.summary["mae"].notna().all()


def test_run_eval_refuses_a_dirty_tree(monkeypatch, capsys):
    from scripts import run_eval

    monkeypatch.setattr(run_eval, "git", lambda *a: " M backend/app/tires/evaluation.py" if a[0] == "status" else "abc")
    assert run_eval.main(["--no-mlflow"]) == 2
    assert "dirty" in capsys.readouterr().err
