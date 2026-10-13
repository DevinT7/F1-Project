"""Degradation-per-lap estimate for the tree forecasters (combined adjusted trend).

Deviation from the plan (documented in docs/tire_model_report.md): the plan differences the
model against a counterfactual with fuel_proxy held fixed. In every training row the features
age_gain (laps since the anchor) and fuel_drop (fuel burned since the anchor) are IDENTICAL, both
equal to laps elapsed, so a tree may split on either and that counterfactual is arbitrary
(test_age_gain_and_fuel_drop_are_identical_in_every_row). What the tree does identify is how
lap time evolves within a stint: wear MINUS fuel burn-off. Fuel is identified across stints by
Baseline B (the tyre-age reset at each stop), so

    degradation ≈ d(predicted lap time)/d(lap) over the next k laps  +  β_fuel(Baseline B)

with both pieces fitted on the same training races. Uncertainty: bootstrap over training races
(≥ 30 resamples, seeded). Estimates outside −0.05…+0.5 s/lap carry implausible_estimate.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .config import SEED
from .features import feature_rows
from .models.base import DegradationEstimate, StintContext
from .models.forecasting import _pooled_fit
from .models.tree_models import TreeForecaster

SANITY_BAND = (-0.05, 0.5)
HORIZON = 5


def next_laps(history: pd.DataFrame, k: int = HORIZON) -> pd.DataFrame:
    """The next k laps' KNOWN context from the last history lap (age +1, fuel −1 per lap)."""
    h = history.sort_values("lap_number")
    last = h.iloc[-1]
    steps = np.arange(1, k + 1)
    future = pd.DataFrame([last] * k).reset_index(drop=True)
    future["lap_number"] = int(last["lap_number"]) + steps
    future["tyre_age"] = int(last["tyre_age"]) + steps
    future["fuel_proxy"] = np.maximum(int(last["fuel_proxy"]) - steps, 0)
    future["lap_time_s"] = np.nan
    return future


def within_stint_slope(model: TreeForecaster, history: pd.DataFrame, k: int = HORIZON) -> float:
    rows = feature_rows(history, next_laps(history, k))
    pred = model.predict_rows(rows)
    return float(np.polyfit(np.arange(1, len(pred) + 1), pred, 1)[0])


def fuel_slope(train: pd.DataFrame) -> float:
    fit = _pooled_fit(train)
    return float(fit.beta_fuel) if np.isfinite(fit.beta_fuel) else 0.0


def estimate(model: TreeForecaster, train: pd.DataFrame, ctx: StintContext, *, n_boot: int = 0,
             make_model: Callable[[], TreeForecaster] | None = None) -> DegradationEstimate:
    """Point estimate from the fitted model; with n_boot > 0 a bootstrap CI over training races."""
    method = f"{model.name}_within_stint_plus_baseline_b_fuel"
    if len(ctx.laps) < 3:
        return DegradationEstimate.unavailable(method, len(ctx.laps), "insufficient_laps")
    value = within_stint_slope(model, ctx.laps) + fuel_slope(train)
    lo = hi = None
    if n_boot:
        factory = make_model or (lambda: TreeForecaster(model.kind, identity=model.identity, k_step=model.k_step,
                                                         hyperparams=model.hyperparams))
        races = sorted(train["session_key"].astype(str).unique())
        rng = np.random.default_rng(SEED)
        draws = []
        for _ in range(n_boot):
            pick = rng.choice(races, size=len(races), replace=True)
            sample = pd.concat([train[train["session_key"] == r].assign(session_key=f"{r}#{i}")
                                for i, r in enumerate(pick)], ignore_index=True)
            m = factory()
            m.fit(sample)
            draws.append(within_stint_slope(m, ctx.laps) + fuel_slope(sample))
        lo, hi = float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))
    flags = () if SANITY_BAND[0] <= value <= SANITY_BAND[1] else ("implausible_estimate",)
    return DegradationEstimate(value, lo, hi, method, len(ctx.laps), flags)
