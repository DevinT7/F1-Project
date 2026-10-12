"""Phase 6B — parameter recovery and flags for Baseline A (per-stint) and B (pooled FE).

Written before the model code. Ground truth comes from app.tires.synthetic: SOFT 0.12, MEDIUM
0.08, HARD 0.05 s/lap linear degradation, fuel 0.06 s per lap of fuel, noise 0.15 s.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.tires.models.base import StintContext
from app.tires.models.baseline_fixed import BaselineFixed
from app.tires.models.baseline_stint import BaselineStint
from app.tires.synthetic import TrueParams, aligned_plans, make_race

TRUE = TrueParams()
COMPOUNDS = ("SOFT", "MEDIUM", "HARD")


def ctx_for(df, compound: str, age: float = 10.0) -> StintContext:
    row = df[df["compound"] == compound].iloc[0]
    stint = df[(df["driver"] == row["driver"]) & (df["stint"] == row["stint"])]
    return StintContext(session_key=row["session_key"], driver=row["driver"], stint=int(row["stint"]),
                        compound=compound, tyre_age=age, laps=stint)


# ---------------------------------------------------------------- Baseline B (pooled fixed effects)
def test_b_noise_free_recovers_degradation_and_fuel_exactly():
    df = make_race(seed=11, noise_sd=0.0)
    model = BaselineFixed()
    model.fit(df)
    fit = model.fits[df["session_key"].iloc[0]]
    for c in COMPOUNDS:
        assert fit.beta_age[c] == pytest.approx(TRUE.deg_linear[c], abs=0.002), c
    assert fit.beta_fuel == pytest.approx(TRUE.fuel_slope, rel=0.05)
    assert "confounded_fuel_age" not in fit.flags


def test_b_noisy_recovery_over_50_seeds_bias_and_coverage():
    estimates: dict[str, list[float]] = {c: [] for c in COMPOUNDS}
    covered: dict[str, list[bool]] = {c: [] for c in COMPOUNDS}
    for seed in range(50):
        df = make_race(seed=1000 + seed)
        model = BaselineFixed()
        model.fit(df)
        for c in COMPOUNDS:
            est = model.degradation_per_lap(ctx_for(df, c))
            estimates[c].append(est.value_s_per_lap)
            covered[c].append(est.ci_low <= TRUE.deg_linear[c] <= est.ci_high)
    for c in COMPOUNDS:
        assert abs(np.mean(estimates[c]) - TRUE.deg_linear[c]) <= 0.01, c
        assert 0.90 <= np.mean(covered[c]) <= 0.99, (c, np.mean(covered[c]))


def test_b_quadratic_recovery_noise_free():
    params = TrueParams(deg_quadratic={"SOFT": 0.002, "MEDIUM": 0.001, "HARD": 0.0005})
    df = make_race(seed=12, noise_sd=0.0, params=params)
    model = BaselineFixed(quadratic=True)
    model.fit(df)
    for c in COMPOUNDS:
        est = model.degradation_per_lap(ctx_for(df, c, age=10.0))
        truth = params.deg_linear[c] + 2 * params.deg_quadratic[c] * 10.0
        assert est.value_s_per_lap == pytest.approx(truth, abs=0.002), c


def test_b_collinear_case_is_flagged():
    df = make_race(seed=13, plans=aligned_plans(20, 57))  # every driver stops on the same laps
    model = BaselineFixed()
    model.fit(df)
    fit = model.fits[df["session_key"].iloc[0]]
    assert "confounded_fuel_age" in fit.flags
    est = model.degradation_per_lap(ctx_for(df, "MEDIUM"))
    assert "confounded_fuel_age" in est.flags


def test_b_records_age_fuel_correlation():
    df = make_race(seed=14)
    model = BaselineFixed()
    model.fit(df)
    fit = model.fits[df["session_key"].iloc[0]]
    assert -1.0 <= fit.corr_age_fuel <= 1.0 and abs(fit.corr_age_fuel) < 0.95


def test_b_hidden_track_evolution_is_absorbed_by_the_fuel_term_not_degradation():
    # linear track evolution is collinear with fuel burn, so it lands in beta_fuel ("combined
    # adjusted trend"), while the degradation slopes stay correct
    df = make_race(seed=15, noise_sd=0.0, params=TrueParams(track_evo_per_lap=-0.02))
    model = BaselineFixed()
    model.fit(df)
    fit = model.fits[df["session_key"].iloc[0]]
    for c in COMPOUNDS:
        assert fit.beta_age[c] == pytest.approx(TRUE.deg_linear[c], abs=0.002)
    assert fit.beta_fuel == pytest.approx(TRUE.fuel_slope + 0.02, rel=0.05)


def test_b_predictions_and_intervals_are_deterministic():
    df = make_race(seed=16)
    a, b = BaselineFixed(), BaselineFixed()
    a.fit(df)
    b.fit(df)
    np.testing.assert_array_equal(a.predict(df), b.predict(df))
    lo, hi = a.predict_interval(df, alpha=0.05)
    assert np.all(lo < a.predict(df)) and np.all(a.predict(df) < hi)


def test_b_pooled_mode_across_races():
    df = __import__("pandas").concat([make_race(seed=20, session_key="2099_01_R"),
                                      make_race(seed=21, session_key="2099_02_R")], ignore_index=True)
    model = BaselineFixed(pooled=True)
    model.fit(df)
    assert set(model.fits) == {"pooled"}
    for c in COMPOUNDS:
        assert model.fits["pooled"].beta_age[c] == pytest.approx(TRUE.deg_linear[c], abs=0.01)


def test_the_specs_driver_stint_intercepts_cannot_identify_fuel():
    """Why B uses driver intercepts: with one intercept per (driver, stint), tyre_age and
    fuel_proxy are exactly collinear inside every group, so the design is rank-deficient."""
    df = make_race(seed=17, noise_sd=0.0)
    from app.tires.models.baseline_fixed import design_rank_deficit
    assert design_rank_deficit(df, intercepts="driver_stint") >= 1
    assert design_rank_deficit(df, intercepts="driver") == 0


# ---------------------------------------------------------------- Baseline A (per-stint linear)
def test_a_recovers_degradation_per_stint_noise_free():
    df = make_race(seed=30, noise_sd=0.0)
    model = BaselineStint()
    model.fit(df)
    for c in COMPOUNDS:
        est = model.degradation_per_lap(ctx_for(df, c))
        assert est.value_s_per_lap == pytest.approx(TRUE.deg_linear[c], abs=0.002), c
        assert est.method == "baseline_stint_fuel_adjusted"


def test_a_noisy_mean_over_50_seeds():
    vals = {c: [] for c in COMPOUNDS}
    for seed in range(50):
        df = make_race(seed=2000 + seed)
        model = BaselineStint()
        model.fit(df)
        for c in COMPOUNDS:
            est = model.degradation_per_lap(ctx_for(df, c))
            if est.value_s_per_lap is not None:
                vals[c].append(est.value_s_per_lap)
    for c in COMPOUNDS:
        assert abs(np.mean(vals[c]) - TRUE.deg_linear[c]) <= 0.01, c


def test_a_never_extrapolates_from_fewer_than_5_clean_laps():
    df = make_race(seed=31, short_stints=True, keep_short_stints=True)  # 6A's diagnostics option
    model = BaselineStint()
    model.fit(df)
    short = df.groupby(["driver", "stint"]).size()
    driver, stint = short[short < 5].index[0]
    laps = df[(df["driver"] == driver) & (df["stint"] == stint)]
    est = model.degradation_per_lap(StintContext(session_key=laps["session_key"].iloc[0], driver=driver,
                                                 stint=int(stint), compound=laps["compound"].iloc[0],
                                                 tyre_age=20.0, laps=laps))
    assert est.value_s_per_lap is None and est.ci_low is None and "insufficient_laps" in est.flags
    assert np.isnan(model.predict(laps)).all()


def test_a_flags_high_variance_stints():
    df = make_race(seed=32, noise_sd=0.8)
    model = BaselineStint()
    model.fit(df)
    flags = [f for e in model.estimates.values() for f in e.flags]
    assert "high_variance" in flags


def test_a_specs_two_regressor_form_is_rank_deficient_within_a_stint():
    df = make_race(seed=33, noise_sd=0.0)
    stint = df[(df["driver"] == df["driver"].iloc[0]) & (df["stint"] == 1)]
    x = np.column_stack([np.ones(len(stint)), stint["fuel_proxy"], stint["tyre_age"]])
    assert np.linalg.matrix_rank(x) == 2  # three columns, rank two: fuel and age cannot be separated


def test_a_deterministic():
    df = make_race(seed=34)
    a, b = BaselineStint(), BaselineStint()
    a.fit(df)
    b.fit(df)
    np.testing.assert_array_equal(a.predict(df), b.predict(df))
