"""Phase 6D — fold-safe feature pipeline and the tree forecasters' leakage guarantees.

Written before the pipeline. Races are synthetic, with distinct circuits/drivers per race so the
encoder tests can see categories that exist only in a test fold.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tires import evaluation as E
from app.tires import features as F
from app.tires.synthetic import make_race


def race(i: int, *, circuit: str | None = None) -> pd.DataFrame:
    df = make_race(seed=300 + i, session_key=f"{2023 + i // 6}_{(i % 6) + 1:02d}_R")
    df["circuit"] = circuit or f"Circuit{i}"
    return df


FRAME = pd.concat([race(i) for i in range(8)], ignore_index=True)


def long_stint(df: pd.DataFrame) -> pd.DataFrame:
    sizes = df.groupby(["driver", "stint"]).size()
    d, s = sizes[sizes >= 18].index[0]
    return df[(df["driver"] == d) & (df["stint"] == s)].sort_values("lap_number").reset_index(drop=True)


# ---------------------------------------------------------------- feature rows
def test_feature_rows_anchor_on_recent_pace_and_project_known_columns():
    stint = long_stint(FRAME[FRAME["session_key"] == "2023_01_R"])
    history, future = E.forecast_inputs(stint, k=8, h=5)
    rows = F.feature_rows(history, future)
    assert len(rows) == 5
    assert rows["steps_ahead"].tolist() == [1, 2, 3, 4, 5]
    assert (rows["anchor_pace"] == history["lap_time_s"].iloc[-3:].mean()).all()
    # stint_lap_index is projected from the cut, never read from (exclusion-revealing) future rows
    assert rows["stint_lap_index"].tolist() == [int(history["stint_lap_index"].iloc[-1]) + i for i in range(1, 6)]
    assert rows["tyre_age"].tolist() == future["tyre_age"].astype(int).tolist()
    assert (rows["compound_ord"] == F.COMPOUND_ORD[stint["compound"].iloc[0]]).all()
    assert (rows["track_temp_c"] == history["track_temp_c"].iloc[-1]).all()  # frozen at the cut


def test_training_rows_match_forecast_time_rows_exactly():
    """The same function builds training rows and forecast rows (no train/serve skew)."""
    one = FRAME[FRAME["session_key"] == "2023_01_R"]
    X, y, groups = F.training_rows(one)
    stint = long_stint(one)
    history, future = E.forecast_inputs(stint, k=7, h=10)
    expect = F.feature_rows(history, future)
    sel = X[(X["driver"] == stint["driver"].iloc[0]) & (X["stint"] == stint["stint"].iloc[0]) & (X["cut"] == 7)]
    pd.testing.assert_frame_equal(sel.drop(columns=["cut"]).reset_index(drop=True), expect.reset_index(drop=True),
                                  check_dtype=False)
    target = stint["lap_time_s"].iloc[7:17].to_numpy() - history["lap_time_s"].iloc[-3:].mean()
    np.testing.assert_allclose(y[sel.index], target)
    assert set(groups) == {"2023_01_R"}


# ---------------------------------------------------------------- fold-safe encoding
def test_encoder_never_sees_the_test_fold():
    fold = next(E.split_generator(FRAME, "loro"))
    X_train, _, _ = F.training_rows(fold.train)
    X_test, _, _ = F.training_rows(fold.test)
    enc = F.FoldEncoder(identity=True).fit(X_train)
    test_only = set(X_test["circuit"]) - set(X_train["circuit"])
    assert test_only, "the held-out race must have a circuit the training races do not"
    assert not test_only & set(enc.categories_["circuit"])
    assert set(enc.categories_["circuit"]) == set(X_train["circuit"])
    Xt = enc.transform(X_test)   # unseen categories are handled, not learned
    assert len(Xt) == len(X_test)
    assert not any(c.startswith("circuit=") and c.split("=", 1)[1] in test_only for c in enc.columns_)


def test_identity_variant_controls_driver_and_team():
    X, _, _ = F.training_rows(FRAME[FRAME["session_key"] == "2023_01_R"])
    base = F.FoldEncoder(identity=False).fit(X)
    ident = F.FoldEncoder(identity=True).fit(X)
    assert not any(c.startswith(("driver=", "team=")) for c in base.columns_)
    assert any(c.startswith("driver=") for c in ident.columns_) and any(c.startswith("team=") for c in ident.columns_)


def test_native_categorical_encoding_uses_train_categories_only():
    fold = next(E.split_generator(FRAME, "loro"))
    X_train, _, _ = F.training_rows(fold.train)
    X_test, _, _ = F.training_rows(fold.test)
    enc = F.FoldEncoder(identity=False, native_categorical=True).fit(X_train)
    out = enc.transform(X_test)
    assert out["circuit"].dtype.name == "category"
    assert list(out["circuit"].cat.categories) == sorted(set(X_train["circuit"]))
    assert out["circuit"].isna().all()  # the held-out circuit is unknown, not invented


# ---------------------------------------------------------------- grouped CV for tuning
def test_grouped_folds_never_split_a_race():
    X, _, groups = F.training_rows(FRAME)
    seen: dict[str, int] = {}
    for i, (_tr, va) in enumerate(F.grouped_folds(groups, n_splits=4)):
        for race_key in set(groups[va]):
            assert race_key not in seen, f"{race_key} validated in two folds"
            seen[race_key] = i
        assert set(groups[_tr]).isdisjoint(set(groups[va]))
    assert set(seen) == set(groups)


# ---------------------------------------------------------------- holdout
def test_holdout_is_the_last_three_races_by_date():
    dev, holdout = E.split_holdout(FRAME, n=3)
    order = E.race_order(FRAME)
    assert sorted(holdout["session_key"].unique()) == sorted(order[-3:])
    assert set(dev["session_key"]).isdisjoint(set(holdout["session_key"]))
    assert len(dev) + len(holdout) == len(FRAME)


# ---------------------------------------------------------------- tree forecasters keep the 6C boundary
@pytest.mark.parametrize("name", ["random_forest", "gradient_boosting", "xgboost", "lightgbm"])
@pytest.mark.parametrize("identity", [False, True])
def test_tree_forecasters_respect_the_information_boundary(name, identity):
    from app.tires.models.tree_models import make_tree_forecaster

    test_key = "2023_01_R"
    model = make_tree_forecaster(name, identity=identity)
    model.fit(FRAME[FRAME["session_key"] != test_key])
    stint = long_stint(FRAME[FRAME["session_key"] == test_key])
    k, h = 8, 5
    before = model.forecast(*E.forecast_inputs(stint, k, h)).mean
    mutated = stint.copy()
    after_k = mutated.index[k:]
    rng = np.random.default_rng(1)
    for col in ("lap_time_s", "prev_lap_time_s", "track_temp_c", "air_temp_c"):
        mutated.loc[after_k, col] = rng.normal(500, 50, size=len(after_k))
    for col in ("position", "laps_since_neutralization", "stint_lap_index"):
        mutated.loc[after_k, col] = rng.integers(0, 99, size=len(after_k))
    np.testing.assert_array_equal(before, model.forecast(*E.forecast_inputs(mutated, k, h)).mean)
    poked = stint.copy()
    poked.loc[poked.index[:k], "lap_time_s"] += 1.0
    assert not np.allclose(model.forecast(*E.forecast_inputs(poked, k, h)).mean, before)
