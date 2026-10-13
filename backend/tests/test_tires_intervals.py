"""Phase 6D — intervals (conformal, quantile), the tree degradation estimator, monotone constraints."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tires import degradation as D
from app.tires import evaluation as E
from app.tires.features import training_rows
from app.tires.models.base import StintContext
from app.tires.models.intervals import QuantileLightGBM, SplitConformal, conformal_quantile
from app.tires.models.persistence import PersistenceModel
from app.tires.models.tree_models import TreeForecaster
from app.tires.synthetic import TrueParams, make_race


def races(n: int, seed0: int) -> pd.DataFrame:
    keys = [f"{2020 + i // 10}_{(i % 10) + 1:02d}_R" for i in range(n)]
    return pd.concat([make_race(seed=seed0 + i, session_key=k) for i, k in enumerate(keys)], ignore_index=True)


def test_conformal_quantile_finite_sample_rank():
    r = np.arange(1, 10, dtype=float)              # n = 9; 80% → rank ceil(10 × 0.8) = 8
    assert conformal_quantile(r, 0.2) == 8.0
    assert conformal_quantile(r, 0.01) == np.inf   # rank 10 > n: no finite guarantee
    assert conformal_quantile(np.array([]), 0.2) == np.inf


def test_conformal_coverage_matches_nominal_over_20_seeds():
    """Exchangeable synthetic races: empirical 80% coverage within ±4 points of nominal."""
    covered = []
    for seed in range(20):
        frame = races(12, 5000 + seed * 20)
        order = E.race_order(frame)
        train = frame[frame["session_key"].isin(order[:10])]
        test = frame[frame["session_key"].isin(order[10:])]
        model = SplitConformal(PersistenceModel(), alpha=0.2, horizons=(5,))
        model.fit(train)
        recs = pd.concat([E.simulate_race(model, g, (5,)) for _, g in test.groupby("session_key")])
        covered.append(E.coverage(recs["lo"], recs["hi"], recs["actual"]))
    assert abs(np.mean(covered) - 0.80) <= 0.04, np.mean(covered)


def test_conformal_calibrates_only_on_the_most_recent_training_races():
    frame = races(10, 600)
    model = SplitConformal(PersistenceModel(), alpha=0.2, calibration_fraction=0.3, horizons=(5,))
    model.fit(frame)
    assert set(model.step_quantiles) == {1, 2, 3, 4, 5}
    assert model.step_quantiles[5] >= model.step_quantiles[1]  # error grows with the horizon


@pytest.mark.parametrize("make", [lambda: SplitConformal(PersistenceModel(), alpha=0.2, horizons=(5,)),
                                  lambda: QuantileLightGBM(alpha=0.2)], ids=["conformal", "quantile"])
def test_intervals_always_contain_the_point_forecast(make):
    frame = races(6, 700)
    model = make()
    model.fit(frame[frame["session_key"] != "2020_06_R"])
    recs = E.simulate_race(model, frame[frame["session_key"] == "2020_06_R"], (5,))
    assert len(recs) and (recs["lo"] <= recs["pred"]).all() and (recs["pred"] <= recs["hi"]).all()


# ---------------------------------------------------------------- tree degradation estimator
def test_age_gain_and_fuel_drop_are_identical_in_every_row():
    """Why the plan's fuel-held-fixed counterfactual is arbitrary for these features."""
    X, _, _ = training_rows(races(2, 800))
    np.testing.assert_allclose(X["age_gain"], X["fuel_drop"])


def _ctx(df: pd.DataFrame, compound: str, k: int = 8) -> StintContext:
    sizes = df[df["compound"] == compound].groupby(["driver", "stint"]).size()
    d, s = sizes[sizes >= 15].index[0]
    stint = df[(df["driver"] == d) & (df["stint"] == s)].sort_values("lap_number")
    return StintContext(stint["session_key"].iloc[0], d, int(s), compound, float(stint["tyre_age"].iloc[k - 1]),
                        stint.iloc[:k])


def test_tree_degradation_recovers_a_known_synthetic_slope():
    frame = races(12, 900)
    train, test = frame[frame["session_key"] != "2021_02_R"], frame[frame["session_key"] == "2021_02_R"]
    model = TreeForecaster("lightgbm")
    model.fit(train)
    truth = TrueParams().deg_linear
    for compound in ("MEDIUM", "HARD"):
        est = D.estimate(model, train, _ctx(test, compound))
        assert est.value_s_per_lap == pytest.approx(truth[compound], abs=0.03), compound
        assert "implausible_estimate" not in est.flags


def test_tree_degradation_is_invariant_to_a_constant_lap_time_offset():
    frame = races(8, 950)
    shifted = frame.assign(lap_time_s=frame["lap_time_s"] + 7.5)
    key = "2020_08_R"
    a, b = TreeForecaster("lightgbm"), TreeForecaster("lightgbm")
    a.fit(frame[frame["session_key"] != key])
    b.fit(shifted[shifted["session_key"] != key])
    ea = D.estimate(a, frame[frame["session_key"] != key], _ctx(frame[frame["session_key"] == key], "HARD"))
    eb = D.estimate(b, shifted[shifted["session_key"] != key], _ctx(shifted[shifted["session_key"] == key], "HARD"))
    assert ea.value_s_per_lap == pytest.approx(eb.value_s_per_lap, abs=1e-9)


def test_bootstrap_ci_brackets_the_estimate():
    frame = races(6, 980)
    key = "2020_06_R"
    train = frame[frame["session_key"] != key]
    model = TreeForecaster("xgboost")
    model.fit(train)
    est = D.estimate(model, train, _ctx(frame[frame["session_key"] == key], "HARD"), n_boot=30)
    assert est.ci_low is not None and est.ci_high is not None and est.ci_low <= est.ci_high


@pytest.mark.parametrize("slope, flagged", [(0.8, True), (-0.2, True), (0.1, False)])
def test_estimates_outside_the_sanity_band_are_flagged(monkeypatch, slope, flagged):
    frame = races(4, 990)
    model = TreeForecaster("xgboost")
    model.fit(frame)
    monkeypatch.setattr(D, "within_stint_slope", lambda m, h, k=5: slope)
    monkeypatch.setattr(D, "fuel_slope", lambda train: 0.0)
    est = D.estimate(model, frame, _ctx(frame, "HARD"))
    assert ("implausible_estimate" in est.flags) is flagged


# ---------------------------------------------------------------- monotone constraint
@pytest.mark.parametrize("kind", ["lightgbm", "xgboost"])
def test_monotone_model_never_predicts_a_decreasing_age_effect(kind):
    frame = races(6, 990)
    model = TreeForecaster(kind, monotone_age=True)
    model.fit(frame)
    stint = _ctx(frame, "HARD").laps
    base = D.feature_rows(stint, D.next_laps(stint, 1))
    grid = pd.concat([base] * 40, ignore_index=True)
    grid["tyre_age"] = np.arange(1, 41)
    grid["age_gain"] = grid["tyre_age"] - float(stint["tyre_age"].iloc[-3:].mean())
    pred = model.predict_rows(grid)
    assert np.all(np.diff(pred) >= -1e-9)
