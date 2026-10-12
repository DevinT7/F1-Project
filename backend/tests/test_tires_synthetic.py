"""Phase 6B — synthetic race generator (written before the models)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.tires.config import DATASET_COLUMNS
from app.tires.synthetic import TrueParams, aligned_plans, make_race, staggered_plans


def test_columns_match_the_6a_dataset():
    df = make_race(seed=1)
    assert list(df.columns) == list(DATASET_COLUMNS)
    assert df["lap_time_s"].notna().all() and (df["tyre_age"] >= 1).all()
    assert set(df["compound"]) <= {"SOFT", "MEDIUM", "HARD"}
    assert not df["traffic_known"].any()


def test_deterministic_given_seed():
    pd.testing.assert_frame_equal(make_race(seed=7), make_race(seed=7))
    assert not make_race(seed=7)["lap_time_s"].equals(make_race(seed=8)["lap_time_s"])


def test_staggered_pit_laps_break_the_age_lap_collinearity():
    df = make_race(seed=3)
    corr = np.corrcoef(df["tyre_age"], df["lap_number"])[0, 1]
    assert abs(corr) < 0.9, corr
    first_stops = {plan[0][1] for plan in staggered_plans(20, 57, np.random.default_rng(3))}
    assert len(first_stops) >= 5  # genuinely staggered


def test_noise_free_lap_times_follow_the_formula_exactly():
    p = TrueParams()
    df = make_race(seed=2, noise_sd=0.0)
    # every lap minus the known fuel + compound + degradation terms leaves only the driver base
    residual = (df["lap_time_s"] - p.fuel_slope * df["fuel_proxy"]
                - df["compound"].map(p.compound_offset)
                - df["compound"].map(p.deg_linear) * df["tyre_age"]
                - df["compound"].map(p.deg_quadratic) * df["tyre_age"] ** 2)
    assert (residual.groupby(df["driver"]).std() < 1e-9).all()


def test_exclusions_really_run_lap1_pit_laps_and_safety_car_are_gone():
    raw = make_race(seed=4, safety_car_laps=(25, 26), clean=False)
    clean = make_race(seed=4, safety_car_laps=(25, 26))
    assert {25, 26} <= set(raw["lap_number"]) and not clean["lap_number"].isin([1, 25, 26]).any()
    # SC laps are far off the pace in the raw table
    assert raw.loc[raw["lap_number"] == 25, "lap_time_s"].min() > raw["lap_time_s"].median() + 15
    assert (clean["laps_since_neutralization"][clean["lap_number"] == 27] == 1).all()


def test_switches():
    assert make_race(seed=5, noise_sd=0.6)["lap_time_s"].std() > make_race(seed=5, noise_sd=0.0)["lap_time_s"].std()
    short = make_race(seed=5, short_stints=True, clean=False)
    lengths = short.groupby(["driver", "stint"]).size()
    assert (lengths < 5).any()  # some stints too short to model
    evo = make_race(seed=5, noise_sd=0.0, params=TrueParams(track_evo_per_lap=-0.02))
    flat = make_race(seed=5, noise_sd=0.0)
    gap = (evo["lap_time_s"] - flat["lap_time_s"]).to_numpy()
    np.testing.assert_allclose(gap, -0.02 * (evo["lap_number"].to_numpy() - 1), atol=1e-9)


def test_aligned_plans_have_identical_pit_laps():
    plans = aligned_plans(20, 57)
    assert len({tuple(p) for p in plans}) == 1


@pytest.mark.parametrize("bad", [dict(n_laps=10), dict(n_drivers=0)])
def test_rejects_unusable_races(bad):
    with pytest.raises(ValueError):
        make_race(seed=1, **bad)
