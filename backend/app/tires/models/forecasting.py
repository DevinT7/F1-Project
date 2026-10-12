"""The forecasting interface the 6C harness drives, and the 6B baselines as forecasters.

    fit(train)                    training races only (from split_generator)
    forecast(history, future)     history = the test stint's laps up to the cut; future = the
                                  laps to predict, with unknowable columns already masked

Leakage note: in 6B, Baseline A borrowed β_fuel from Baseline B fitted on the SAME race. In a
forecast that would use the test race's future laps, so every coefficient here comes from the
training races only (a pooled Baseline B), and the test stint contributes only its history.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd
from scipy import stats

from .base import FloatArray
from .baseline_fixed import BaselineFixed, FixedFit


@dataclass(frozen=True)
class Forecast:
    mean: FloatArray
    lo: FloatArray | None = None   # 95% prediction interval, when the model provides one
    hi: FloatArray | None = None


class ForecastModel(Protocol):
    name: str

    def params(self) -> dict[str, object]: ...
    def fit(self, train: pd.DataFrame) -> None: ...
    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast: ...


_POOLED_CACHE: dict[tuple[tuple[str, ...], int, float], FixedFit] = {}


def _pooled_fit(train: pd.DataFrame) -> FixedFit:
    """Pooled Baseline B on the training races. Cached per training set (A and B share it in a fold);
    the key includes the row count and lap-time sum so a different frame never hits a stale entry."""
    key = (tuple(sorted(train["session_key"].astype(str).unique())), len(train), float(train["lap_time_s"].sum()))
    if key not in _POOLED_CACHE:
        model = BaselineFixed(pooled=True)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(train)
        if len(_POOLED_CACHE) > 64:
            _POOLED_CACHE.clear()
        _POOLED_CACHE[key] = model.fits["pooled"]
    return _POOLED_CACHE[key]


def _temp_coef(fit: FixedFit) -> float:
    return float(fit.result.params.get("track_temp_c", 0.0)) if fit.temp_term else 0.0


class PooledFixedEffects:
    """Baseline B as a forecaster. Fuel, temperature and per-compound tyre-age coefficients come
    from a pooled fit on the training races; the stint's level is anchored on its own history
    (a new (race, driver) has no fitted intercept)."""

    name = "pooled_fixed_effects"

    def __init__(self) -> None:
        self.fit_: FixedFit | None = None

    def params(self) -> dict[str, object]:
        return {"fit": "pooled", "intercepts": "race:driver", "anchor": "history mean"}

    def fit(self, train: pd.DataFrame) -> None:
        self.fit_ = _pooled_fit(train)

    def _fixed(self, df: pd.DataFrame, compound: str) -> FloatArray:
        assert self.fit_ is not None
        f = self.fit_
        age = df["tyre_age"].to_numpy(dtype=float)
        slope = f.beta_age.get(compound, 0.0)
        slope = 0.0 if not np.isfinite(slope) else slope
        out: FloatArray = (f.beta_fuel * df["fuel_proxy"].to_numpy(dtype=float) + slope * age
                           + f.gamma.get(compound, 0.0) * age ** 2
                           + _temp_coef(f) * df["track_temp_c"].to_numpy(dtype=float))
        return out

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        if self.fit_ is None:
            raise RuntimeError("fit() first")
        compound = str(history["compound"].iloc[0])
        level = float(np.mean(history["lap_time_s"].to_numpy(dtype=float) - self._fixed(history, compound)))
        mean: FloatArray = (level + self._fixed(future, compound)).astype(np.float64)
        return Forecast(mean=mean)


class StintLinearExtrapolation:
    """Baseline A as a forecaster: OLS of the fuel-adjusted history lap times on tyre age,
    extrapolated linearly, with a t-based 95% prediction interval. β_fuel comes from a pooled
    Baseline B on the training races."""

    name = "stint_linear_extrapolation"

    def __init__(self, min_laps: int = 5) -> None:
        self.min_laps = min_laps
        self.beta_fuel = 0.0

    def params(self) -> dict[str, object]:
        return {"min_laps": self.min_laps, "fuel": "pooled Baseline B on training races"}

    def fit(self, train: pd.DataFrame) -> None:
        fit = _pooled_fit(train)
        self.beta_fuel = fit.beta_fuel if np.isfinite(fit.beta_fuel) and fit.beta_fuel > 0 else 0.0

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        age = history["tyre_age"].to_numpy(dtype=float)
        y = history["lap_time_s"].to_numpy(dtype=float) - self.beta_fuel * history["fuel_proxy"].to_numpy(dtype=float)
        f_age = future["tyre_age"].to_numpy(dtype=float)
        f_fuel = self.beta_fuel * future["fuel_proxy"].to_numpy(dtype=float)
        n = len(age)
        sxx = float(((age - age.mean()) ** 2).sum()) if n else 0.0
        if n < self.min_laps or sxx <= 0:  # never extrapolate a slope from too little data
            flat = np.full(len(future), float(np.mean(y[-3:])) if n else np.nan) + f_fuel
            return Forecast(mean=flat)
        slope = float(((age - age.mean()) * (y - y.mean())).sum() / sxx)
        intercept = float(y.mean() - slope * age.mean())
        resid = y - (intercept + slope * age)
        s = float(np.sqrt((resid ** 2).sum() / (n - 2)))
        t = float(stats.t.ppf(0.975, n - 2))
        se = s * np.sqrt(1 + 1 / n + (f_age - age.mean()) ** 2 / sxx)
        mean = intercept + slope * f_age + f_fuel
        return Forecast(mean=mean, lo=mean - t * se, hi=mean + t * se)
