"""Synthetic races with KNOWN degradation: the ground-truth testbed for Phase 6 models.

    lap_time = base_driver + compound_offset_c + fuel_slope × fuel_proxy
               + deg_linear_c × tyre_age + deg_quadratic_c × tyre_age²
               + track_evo_per_lap × (lap_number − 1) + ε,      ε ~ N(0, noise_sd)

Raw laps are generated with pit in/out laps, lap 1 and optional safety-car laps marked the way
FastF1 marks them, then cleaned by the REAL Phase 6A exclusion rules and given the same
derived columns, so the output has exactly the 6A dataset columns.

Pit laps are staggered across drivers by default: with everyone stopping on the same lap, tyre
age, fuel and lap number are confounded and recovery tests would be meaningless.
aligned_plans() builds that hard case deliberately.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import DATASET_COLUMNS, SEED, ExclusionConfig
from .dataset import laps_since, neutralized_laps
from .exclusions import apply_rules

StintPlan = list[tuple[str, int]]  # [(compound, laps), ...] summing to the race length


@dataclass(frozen=True)
class TrueParams:
    fuel_slope: float = 0.06  # s per lap of fuel remaining (heavier → slower)
    deg_linear: dict[str, float] = field(default_factory=lambda: {"SOFT": 0.12, "MEDIUM": 0.08, "HARD": 0.05})
    deg_quadratic: dict[str, float] = field(default_factory=lambda: {"SOFT": 0.0, "MEDIUM": 0.0, "HARD": 0.0})
    compound_offset: dict[str, float] = field(default_factory=lambda: {"SOFT": 0.0, "MEDIUM": 0.35, "HARD": 0.7})
    track_evo_per_lap: float = 0.0  # hidden term (negative = track getting faster)
    base_mean: float = 92.0
    base_sd: float = 0.4  # driver-to-driver pace spread
    sc_penalty_s: float = 30.0


def _split(n_laps: int, cuts: Sequence[int], compounds: Sequence[str]) -> StintPlan:
    edges = [0, *cuts, n_laps]
    return [(c, edges[i + 1] - edges[i]) for i, c in enumerate(compounds)]


def staggered_plans(n_drivers: int, n_laps: int, rng: np.random.Generator, short_stints: bool = False) -> list[StintPlan]:
    """Realistic mixed 1- and 2-stop strategies with pit laps spread across the race."""
    plans: list[StintPlan] = []
    starts = [("SOFT", "HARD"), ("MEDIUM", "HARD"), ("HARD", "MEDIUM"), ("SOFT", "MEDIUM", "HARD"),
              ("MEDIUM", "HARD", "SOFT"), ("MEDIUM", "MEDIUM", "HARD")]
    for i in range(n_drivers):
        compounds = starts[int(rng.integers(len(starts)))]
        if len(compounds) == 2:
            cuts = [int(rng.integers(int(0.3 * n_laps), int(0.6 * n_laps)))]
        else:
            a = int(rng.integers(int(0.2 * n_laps), int(0.4 * n_laps)))
            cuts = [a, int(rng.integers(a + 8, int(0.8 * n_laps)))]
        if short_stints and i % 4 == 0:  # an extra early stop leaves a stint too short to model
            cuts = [3, *[c for c in cuts if c > 7]]
            compounds = ("SOFT", *compounds[: len(cuts)])
        plans.append(_split(n_laps, cuts, compounds[: len(cuts) + 1]))
    return plans


def aligned_plans(n_drivers: int, n_laps: int) -> list[StintPlan]:
    """The collinear hard case: every driver runs MEDIUM → HARD and stops on the same lap."""
    return [_split(n_laps, [n_laps // 2], ("MEDIUM", "HARD")) for _ in range(n_drivers)]


def make_race(
    n_drivers: int = 20,
    n_laps: int = 57,
    plans: list[StintPlan] | None = None,
    params: TrueParams = TrueParams(),
    noise_sd: float = 0.15,
    seed: int = SEED,
    *,
    short_stints: bool = False,
    safety_car_laps: Sequence[int] = (),
    session_key: str = "2099_01_R",
    clean: bool = True,
    keep_short_stints: bool = False,
) -> pd.DataFrame:
    """A synthetic race in the 6A dataset shape (clean=False: raw laps before exclusions)."""
    if n_drivers < 1 or n_laps < 20:
        raise ValueError("need at least 1 driver and 20 laps")
    rng = np.random.default_rng(seed)
    plans = plans if plans is not None else staggered_plans(n_drivers, n_laps, rng, short_stints)
    bases = rng.normal(params.base_mean, params.base_sd, size=n_drivers)
    # Track temperature: a gentle cooling trend plus a seeded random walk. It must NOT be an
    # exact linear function of lap number, or it would be collinear with fuel_proxy.
    temp = 40.0 - 3.0 * np.arange(n_laps + 1) / n_laps + np.cumsum(rng.normal(0.0, 0.35, size=n_laps + 1))
    sc = set(safety_car_laps)
    rows: list[dict[str, object]] = []
    for d, plan in enumerate(plans):
        lap = 1
        for stint, (compound, length) in enumerate(plan, start=1):
            for age in range(1, length + 1):
                fuel = n_laps - lap
                t = (bases[d] + params.compound_offset[compound] + params.fuel_slope * fuel
                     + params.deg_linear[compound] * age + params.deg_quadratic[compound] * age ** 2
                     + params.track_evo_per_lap * (lap - 1))
                if noise_sd:
                    t += rng.normal(0.0, noise_sd)
                if lap in sc:
                    t += params.sc_penalty_s
                rows.append({
                    "session_key": session_key, "circuit": "Synthetic", "year": int(session_key[:4]),
                    "round": int(session_key[5:7]), "driver": f"D{d + 1:02d}", "team": f"T{d // 2 + 1}",
                    "lap_number": lap, "stint": stint, "compound": compound, "tyre_age": age,
                    "fresh_tyre": True, "lap_time_s": float(t), "track_temp_c": float(temp[lap]),
                    "air_temp_c": 25.0, "rainfall": False, "position": d + 1,
                    "race_laps_total": n_laps, "fuel_proxy": fuel, "traffic_known": False,
                    # FastF1-style markers the exclusion rules read
                    "track_status": "4" if lap in sc else "1",
                    "pit_in_time_s": 1.0 if age == length and stint < len(plan) else None,
                    "pit_out_time_s": 1.0 if age == 1 and stint > 1 else None,
                    "is_accurate": True, "deleted": False,
                })
                lap += 1
    raw = pd.DataFrame(rows)
    if not clean:
        raw["stint_lap_index"] = raw.groupby(["driver", "stint"]).cumcount() + 1
        raw["prev_lap_time_s"] = raw.groupby(["driver", "stint"])["lap_time_s"].shift(1)
        raw["laps_since_neutralization"] = laps_since(raw["lap_number"], neutralized_laps(raw))
        return raw
    kept = apply_rules(raw, ExclusionConfig(keep_short_stints=keep_short_stints)).kept.sort_values(["driver", "stint", "lap_number"]).copy()
    by_stint = kept.groupby(["driver", "stint"], sort=False)
    kept["stint_lap_index"] = by_stint.cumcount() + 1
    kept["prev_lap_time_s"] = by_stint["lap_time_s"].shift(1)
    kept["laps_since_neutralization"] = laps_since(kept["lap_number"], neutralized_laps(raw))
    out = kept[list(DATASET_COLUMNS)].reset_index(drop=True)
    for col in ("stint", "tyre_age", "position"):
        out[col] = out[col].astype("Int64")
    return out
