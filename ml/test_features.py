"""
Offline checks for build_dataset's feature engineering. No FastF1, no network.

    python test_features.py        (or: pytest ml/test_features.py)

Plants a known degradation slope in a synthetic race and asserts the pipeline recovers
it after fuel correction, and that each derived feature does what its docstring claims.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from build_dataset import (CLEAR_TRACK_S, FUEL_EFFECT_S_PER_LAP, MIN_STINT_LAPS,
                           TARGET, _clean_and_engineer, stint_slopes)

PLANTED_SLOPE = 0.08   # s/lap of real degradation
BASE_LAP = 90.0


def _race(n_laps: int = 30, drivers: tuple[str, ...] = ("VER", "HAM")) -> pd.DataFrame:
    """One stint each, clean, with the planted slope buried under fuel burn-off."""
    rows = []
    for d_i, drv in enumerate(drivers):
        for lap in range(1, n_laps + 1):
            rows.append({
                "year": 2024, "round": 1, "circuit": "Test GP",
                "driver": drv, "team": f"Team{d_i}", "compound": "MEDIUM",
                "tyre_age": float(lap), "stint": 1.0, "lap_number": float(lap),
                # raw time = base + degradation + fuel penalty (heavier early = slower)
                "lap_time_s": (BASE_LAP + d_i * 0.5 + PLANTED_SLOPE * lap
                               + FUEL_EFFECT_S_PER_LAP * (n_laps - lap)),
                "lap_start_s": 1000.0 + lap * 90.0 + d_i * 2.0,
                "track_status": "1", "is_pit": False, "is_invalid": False,
                "track_temp": 40.0, "air_temp": 28.0,
            })
    return pd.DataFrame(rows)


def test_recovers_planted_slope() -> None:
    out = _clean_and_engineer(_race())
    slope = float(np.polyfit(out["tyre_age"], out[TARGET], 1)[0])
    assert abs(slope - PLANTED_SLOPE) < 1e-6, f"fuel correction off: got {slope}"


def test_drops_non_green_pit_and_invalid_laps() -> None:
    df = _race()
    df.loc[(df.driver == "VER") & (df.lap_number == 10), "track_status"] = "4"  # SC
    df.loc[(df.driver == "VER") & (df.lap_number == 11), "is_pit"] = True
    df.loc[(df.driver == "VER") & (df.lap_number == 12), "is_invalid"] = True
    out = _clean_and_engineer(df)
    ver = set(out.loc[out.driver == "VER", "lap_number"])
    assert not ({10, 11, 12} & ver), f"dirty laps survived: {sorted(ver)}"
    assert 10 in set(out.loc[out.driver == "HAM", "lap_number"]), "dropped the wrong car"


def test_drops_laps_far_slower_than_stint_median() -> None:
    df = _race()
    df.loc[(df.driver == "HAM") & (df.lap_number == 20), "lap_time_s"] += 12.0
    out = _clean_and_engineer(df)
    assert 20 not in set(out.loc[out.driver == "HAM", "lap_number"])


def test_previous_lap_pace_is_strictly_earlier() -> None:
    out = _clean_and_engineer(_race()).sort_values(["driver", "lap_number"])
    for _, g in out.groupby("driver"):
        assert np.allclose(g["previous_lap_pace"].to_numpy()[1:],
                           g[TARGET].to_numpy()[:-1]), "previous_lap_pace misaligned"
        # first clean lap of the stint has no predecessor and must be dropped
        assert g["lap_number"].min() == 2.0


def test_laps_since_safety_car_counts_from_last_non_green_lap() -> None:
    df = _race()
    df.loc[df.lap_number == 10, "track_status"] = "4"   # session-wide SC on lap 10
    out = _clean_and_engineer(df).set_index(["driver", "lap_number"])
    assert out.loc[("VER", 9.0), "laps_since_safety_car"] == 9.0    # none yet -> since start
    assert out.loc[("VER", 13.0), "laps_since_safety_car"] == 3.0
    assert out.loc[("VER", 30.0), "laps_since_safety_car"] == 20.0


def test_gap_ahead_uses_lap_start_delta_and_flags_the_leader() -> None:
    out = _clean_and_engineer(_race()).set_index(["driver", "lap_number"])
    # VER starts each lap 2.0s before HAM, so VER leads (clear track), HAM trails by 2s
    assert out.loc[("VER", 15.0), "gap_ahead_s"] == CLEAR_TRACK_S
    assert abs(out.loc[("HAM", 15.0), "gap_ahead_s"] - 2.0) < 1e-9


def test_drops_wet_compounds() -> None:
    df = _race()
    df.loc[df.driver == "HAM", "compound"] = "INTERMEDIATE"
    out = _clean_and_engineer(df)
    assert set(out["driver"]) == {"VER"}, "wet running must not reach the training set"


def test_stint_slopes_are_per_stint_not_pooled() -> None:
    """Two drivers with the same degradation but a big constant pace offset. A pooled
    fit would be skewed by the offset; a per-stint fit must recover the planted slope."""
    df = _race(drivers=("VER", "HAM"))
    df.loc[df.driver == "HAM", "lap_time_s"] += 11.0   # different-circuit-sized offset
    s = stint_slopes(_clean_and_engineer(df))
    assert len(s) == 2, f"expected one row per stint, got {len(s)}"
    assert np.allclose(s["slope"], PLANTED_SLOPE), s["slope"].tolist()


def test_stint_slopes_skips_stints_that_are_too_short() -> None:
    assert stint_slopes(_clean_and_engineer(_race(n_laps=MIN_STINT_LAPS))).empty


def test_empty_input_does_not_raise() -> None:
    df = _race()
    df["is_invalid"] = True
    assert _clean_and_engineer(df).empty


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
