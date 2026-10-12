"""Baseline A: per-stint linear degradation on fuel-adjusted lap times.

For each (race, driver, stint) with ≥ min_laps clean laps:
    y = lap_time − β_fuel · fuel_proxy ;   y ~ a + b · tyre_age   (OLS)
degradation = b with a t-based 95% CI from its standard error; forecast = linear extrapolation.

Deviation from the plan (documented in docs/tire_baselines.md): the plan regresses lap_time on
fuel_proxy AND tyre_age within one stint for stints ≥ 8 laps. Inside a stint
tyre_age = const − fuel_proxy exactly, so that design has rank 2, not 3, and the split between
fuel and wear is arbitrary. β_fuel is instead taken from Baseline B fitted on the same race
(or passed in explicitly). Without a usable fuel estimate the raw slope is reported with the
flag fuel_not_adjusted.

Never extrapolates from < min_laps clean laps: those stints get a null estimate and the
insufficient_laps flag, and predict() returns NaN for them.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .base import DegradationEstimate, FloatArray, StintContext
from .baseline_fixed import BaselineFixed

StintKey = tuple[str, str, int]


@dataclass(frozen=True)
class StintLine:
    intercept: float
    slope: float
    se_slope: float
    resid_sd: float
    n: int
    mean_age: float
    sxx: float
    beta_fuel: float


class BaselineStint:
    name = "baseline_stint"

    def __init__(self, *, fuel_slope: float | None = None, min_laps: int = 5, high_variance_ci_s: float = 0.1) -> None:
        self.fuel_slope = fuel_slope
        self.min_laps = min_laps
        self.high_variance_ci_s = high_variance_ci_s
        self.lines: dict[StintKey, StintLine] = {}
        self.estimates: dict[StintKey, DegradationEstimate] = {}
        self.fuel_by_session: dict[str, tuple[float | None, tuple[str, ...]]] = {}

    def _fuel(self, df: pd.DataFrame) -> None:
        self.fuel_by_session = {}
        if self.fuel_slope is not None:
            for key in df["session_key"].unique():
                self.fuel_by_session[str(key)] = (self.fuel_slope, ())
            return
        pooled = BaselineFixed()
        pooled.fit(df)
        for key, fit in pooled.fits.items():
            usable = np.isfinite(fit.beta_fuel) and "confounded_fuel_age" not in fit.flags
            self.fuel_by_session[key] = (fit.beta_fuel if usable else None,
                                         () if usable else ("fuel_from_confounded_fit",))

    def fit(self, df: pd.DataFrame) -> None:
        self._fuel(df)
        self.lines, self.estimates = {}, {}
        for (session, driver, stint), laps in df.groupby(["session_key", "driver", "stint"], sort=True):
            key: StintKey = (str(session), str(driver), int(stint))
            fuel, fuel_flags = self.fuel_by_session.get(key[0], (None, ("fuel_not_adjusted",)))
            method = "baseline_stint_fuel_adjusted" if fuel is not None else "baseline_stint_unadjusted"
            flags = list(fuel_flags) + ([] if fuel is not None else ["fuel_not_adjusted"])
            n = len(laps)
            if n < self.min_laps:
                self.estimates[key] = DegradationEstimate.unavailable(method, n, "insufficient_laps", *flags)
                continue
            age = laps["tyre_age"].to_numpy(dtype=float)
            y = laps["lap_time_s"].to_numpy(dtype=float) - (fuel or 0.0) * laps["fuel_proxy"].to_numpy(dtype=float)
            mean_age = float(age.mean())
            sxx = float(((age - mean_age) ** 2).sum())
            if sxx <= 0:
                self.estimates[key] = DegradationEstimate.unavailable(method, n, "no_age_variation", *flags)
                continue
            slope = float(((age - mean_age) * (y - y.mean())).sum() / sxx)
            intercept = float(y.mean() - slope * mean_age)
            resid = y - (intercept + slope * age)
            resid_sd = float(np.sqrt((resid ** 2).sum() / (n - 2))) if n > 2 else 0.0
            se = resid_sd / np.sqrt(sxx)
            t = float(stats.t.ppf(0.975, n - 2))
            lo, hi = slope - t * se, slope + t * se
            if hi - lo > self.high_variance_ci_s:
                flags.append("high_variance")
            self.lines[key] = StintLine(intercept, slope, float(se), resid_sd, n, mean_age, sxx, fuel or 0.0)
            self.estimates[key] = DegradationEstimate(slope, lo, hi, method, n, tuple(flags))

    def degradation_per_lap(self, ctx: StintContext) -> DegradationEstimate:
        est = self.estimates.get((ctx.session_key, ctx.driver, int(ctx.stint)))
        if est is None:
            return DegradationEstimate.unavailable("baseline_stint_fuel_adjusted", len(ctx.laps), "not_fitted")
        return est

    def predict(self, df: pd.DataFrame) -> FloatArray:
        mean, _lo, _hi = self._predict(df, alpha=0.05)
        return mean

    def predict_interval(self, df: pd.DataFrame, alpha: float = 0.05) -> tuple[FloatArray, FloatArray]:
        _mean, lo, hi = self._predict(df, alpha)
        return lo, hi

    def _predict(self, df: pd.DataFrame, alpha: float) -> tuple[FloatArray, FloatArray, FloatArray]:
        n = len(df)
        mean, lo, hi = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        keys = zip(df["session_key"].astype(str), df["driver"].astype(str), df["stint"].astype(int))
        ages = df["tyre_age"].to_numpy(dtype=float)
        fuels = df["fuel_proxy"].to_numpy(dtype=float)
        for i, key in enumerate(keys):
            line = self.lines.get(key)
            if line is None:
                continue
            m = line.intercept + line.slope * ages[i] + line.beta_fuel * fuels[i]
            se = line.resid_sd * np.sqrt(1 + 1 / line.n + (ages[i] - line.mean_age) ** 2 / line.sxx)
            t = float(stats.t.ppf(1 - alpha / 2, max(line.n - 2, 1)))
            mean[i], lo[i], hi[i] = m, m - t * se, m + t * se
        return mean, lo, hi
