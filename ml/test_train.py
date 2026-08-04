"""
Offline checks for train.py's validation machinery. No parquet, no network.

    python test_train.py        (or: pytest ml/test_train.py)

The thing worth testing here is not the models — it is the harness. A walk-forward that
quietly trains on the future, or a baseline that peeks at the lap it is predicting, will
still print a beautiful table. These plant known answers into synthetic races and assert
the harness reports them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from train import (CAT, MIN_TRAIN_RACES, NUM_DELTA, REF_LAPS, baseline_persistence,
                   baseline_stint_linear, candidates, implied_degradation,
                   make_pipeline, measured_degradation, prepare, train_slice,
                   walk_forward)

PLANTED_SLOPE = 0.08
BASE_LAP = 90.0


def _races(n_races: int = 8, n_laps: int = 20) -> pd.DataFrame:
    """One driver, one stint per race, a planted degradation slope, no noise.

    Circuit names run *backwards* through the alphabet as the season progresses, so any
    code that sorts by circuit name instead of round produces the reverse of the true
    time order — the exact bug this harness used to have.
    """
    rows = []
    for rnd in range(1, n_races + 1):
        circuit = f"{chr(ord('z') - rnd)} Grand Prix"
        for lap in range(1, n_laps + 1):
            rows.append({
                "year": 2024, "round": rnd, "circuit": circuit,
                "driver": "VER", "team": "Red Bull", "compound": "MEDIUM",
                "tyre_age": float(lap), "stint": 1.0, "lap_number": float(lap),
                "fuel_corrected_s": BASE_LAP + PLANTED_SLOPE * lap,
                "previous_lap_pace": BASE_LAP + PLANTED_SLOPE * (lap - 1),
                "track_temp": 40.0, "air_temp": 28.0,
                "laps_since_safety_car": float(lap), "gap_ahead_s": 2.5,
            })
    return prepare(pd.DataFrame(rows))


def test_race_order_is_chronological_not_alphabetical() -> None:
    df = _races()
    order = df.groupby("race")["round"].first().to_numpy()
    assert (np.diff(order) > 0).all(), f"races not in round order: {order}"


def test_train_slice_never_sees_the_test_race_or_later() -> None:
    df = _races(n_races=10)
    for race in range(MIN_TRAIN_RACES, 10):
        train = train_slice(df, race, max_train=3)
        assert train["race"].max() < race, "walk-forward is training on the future"
        assert train["race"].min() >= race - 3, "rolling window is not being honoured"
    # max_train <= 0 means expanding window: everything before the test race
    assert train_slice(df, 9, max_train=0)["race"].nunique() == 9


def test_delta_target_removes_the_stint_level() -> None:
    """Two races with a big constant pace offset — a different circuit, in effect.
    The delta target must be identical for both; that is the whole point of it."""
    df = _races(n_races=2)
    df.loc[df["round"] == 2, "fuel_corrected_s"] += 11.0
    df = prepare(df.drop(columns=["race", "stint_ref_s", "delta", "lap_in_stint"]))
    a = df.loc[df["round"] == 1, "delta"].to_numpy()
    b = df.loc[df["round"] == 2, "delta"].to_numpy()
    assert np.allclose(a, b), "delta target still carries the circuit's pace level"


def test_stint_linear_baseline_recovers_the_planted_slope() -> None:
    df = _races(n_races=1)
    pred = baseline_stint_linear(df, df, "absolute")
    scored = df["lap_in_stint"] >= REF_LAPS       # same rows walk_forward scores
    assert np.allclose(pred[scored.to_numpy()],
                       df.loc[scored, "fuel_corrected_s"]), "baseline mispredicts a "\
        "noiseless straight line — it is not extrapolating the stint's own fit"


def test_stint_linear_baseline_does_not_peek_at_the_lap_it_predicts() -> None:
    """Spike one lap. A baseline fitted on strictly earlier laps cannot see the spike,
    so its prediction for that lap must stay on the pre-spike line."""
    df = _races(n_races=1)
    spike = df["tyre_age"] == 15.0
    df.loc[spike, "fuel_corrected_s"] += 30.0
    pred = baseline_stint_linear(df, df, "absolute")
    at_spike = float(pred[np.flatnonzero(spike.to_numpy())[0]])
    assert abs(at_spike - (BASE_LAP + PLANTED_SLOPE * 15)) < 1e-6, at_spike


def test_persistence_baseline_is_the_previous_lap() -> None:
    df = _races(n_races=1)
    assert np.allclose(baseline_persistence(df, df, "absolute"), df["previous_lap_pace"])
    assert np.allclose(baseline_persistence(df, df, "delta"),
                       df["previous_lap_pace"] - df["stint_ref_s"])


def test_walk_forward_folds_are_scored_after_the_stint_warmup() -> None:
    df = _races(n_races=10, n_laps=40)   # a 3-race window must clear MIN_TRAIN_ROWS
    r = walk_forward(df, baseline_persistence, "delta", max_train=3)
    assert len(r["folds"]) == 10 - MIN_TRAIN_RACES
    assert [f["round"] for f in r["folds"]] == sorted(f["round"] for f in r["folds"])
    # noiseless data, and persistence is exact one lap behind a straight line only up to
    # the planted slope, so the error is the slope itself
    assert abs(r["mae"] - PLANTED_SLOPE) < 1e-6, r["mae"]


def test_implied_degradation_recovers_a_planted_slope() -> None:
    df = _races(n_races=6)
    pipe = make_pipeline(candidates(["linear"])["linear"], NUM_DELTA)
    pipe.fit(df[NUM_DELTA + CAT], df["delta"])
    slope = implied_degradation(pipe, df, NUM_DELTA)
    # laps_since_safety_car moves lockstep with tyre_age here, so a linear model splits
    # the slope between them: the sweep sees tyre_age's share, never more than the whole
    assert 0 < slope <= PLANTED_SLOPE + 1e-9, slope


def test_measured_degradation_is_the_planted_slope() -> None:
    assert abs(measured_degradation(_races(), "absolute") - PLANTED_SLOPE) < 1e-6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
