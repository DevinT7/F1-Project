"""Phase 6D prediction intervals.

QuantileLightGBM   LightGBM quantile regressions at α/2 and 1 − α/2 on the same target
                   (lap time − recent pace), added to the anchor.
SplitConformal     wraps any forecaster. Inside fit(train) the training races are split BY DATE:
                   the most recent `calibration_fraction` of them are held out as calibration,
                   the model is fitted on the earlier ones, and forecasts on the calibration races
                   give absolute residuals PER HORIZON STEP (error grows with the horizon). The
                   interval is prediction ± the finite-sample (1 − α) quantile of those residuals.
                   Calibration data is never the test race and never later than the training cut.

Both guarantee lo ≤ mean ≤ hi (quantile crossings are repaired).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from lightgbm import LGBMRegressor

from ..config import SEED
from ..evaluation import race_order, simulate_race
from ..features import FoldEncoder, feature_rows
from .forecasting import Forecast, ForecastModel
from .tree_models import TreeForecaster, fold_rows

FloatArray = npt.NDArray[np.float64]


def conformal_quantile(residuals: FloatArray, alpha: float) -> float:
    """Finite-sample split-conformal quantile: the ceil((n + 1)(1 − α))-th smallest |residual|."""
    r = np.sort(np.abs(np.asarray(residuals, dtype=float)))
    n = len(r)
    if n == 0:
        return math.inf
    rank = math.ceil((n + 1) * (1 - alpha))
    return float(r[min(rank, n) - 1]) if rank <= n else math.inf


def order_bounds(mean: FloatArray, lo: FloatArray, hi: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Guarantee lo ≤ mean ≤ hi."""
    return np.minimum(lo, mean), np.maximum(hi, mean)


class SplitConformal:
    """Split-conformal intervals around any forecaster, calibrated on earlier races, per step."""

    def __init__(self, base: ForecastModel, *, alpha: float = 0.2, calibration_fraction: float = 0.3,
                 horizons: tuple[int, ...] = (5, 10)) -> None:
        self.base = base
        self.alpha = alpha
        self.calibration_fraction = calibration_fraction
        self.horizons = horizons
        self.name = f"{base.name}+conformal{int(round((1 - alpha) * 100))}"
        self.step_quantiles: dict[int, float] = {}
        self.residuals: dict[int, list[float]] = {}

    def params(self) -> dict[str, object]:
        return {**self.base.params(), "interval": "split-conformal", "alpha": self.alpha,
                "calibration_fraction": self.calibration_fraction}

    def fit(self, train: pd.DataFrame) -> None:
        order = race_order(train)
        n_cal = max(1, int(round(len(order) * self.calibration_fraction)))
        cal_keys = set(order[-n_cal:])  # the most RECENT training races
        keys = train["session_key"].astype(str)
        proper, calibration = train[~keys.isin(cal_keys)], train[keys.isin(cal_keys)]
        self.base.fit(proper)
        recs = simulate_race_many(self.base, calibration, self.horizons)
        self.residuals = {int(s): list((g["actual"] - g["pred"]).abs()) for s, g in recs.groupby("step")}
        self.step_quantiles = {s: conformal_quantile(np.asarray(r), self.alpha) for s, r in self.residuals.items()}

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        mean = self.base.forecast(history, future).mean
        q = np.array([self.step_quantiles.get(i + 1, max(self.step_quantiles.values(), default=math.inf))
                      for i in range(len(mean))])
        lo, hi = order_bounds(mean, mean - q, mean + q)
        return Forecast(mean=mean, lo=lo, hi=hi)


def simulate_race_many(model: ForecastModel, frame: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    parts = [simulate_race(model, g, horizons) for _, g in frame.groupby("session_key", sort=True)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["step", "actual", "pred"])


class QuantileLightGBM:
    """Point forecast from a LightGBM forecaster plus α/2 and 1 − α/2 quantile LightGBMs."""

    def __init__(self, *, alpha: float = 0.2, identity: bool = False, k_step: int = 2, n_jobs: int = 1,
                 hyperparams: dict[str, Any] | None = None) -> None:
        self.alpha = alpha
        self.point = TreeForecaster("lightgbm", identity=identity, k_step=k_step, n_jobs=n_jobs, hyperparams=hyperparams)
        self.identity, self.k_step, self.n_jobs = identity, k_step, n_jobs
        self.hyperparams = dict(hyperparams or {})
        self.name = f"lightgbm+quantile{int(round((1 - alpha) * 100))}"
        self.encoder: FoldEncoder | None = None
        self.lo_model: Any = None
        self.hi_model: Any = None

    def params(self) -> dict[str, object]:
        return {**self.point.params(), "interval": "lightgbm quantile", "alpha": self.alpha}

    def fit(self, train: pd.DataFrame) -> None:
        self.point.fit(train)
        X, y, _ = fold_rows(train, self.k_step)
        self.encoder = FoldEncoder(identity=self.identity, native_categorical=True).fit(X)
        Xt = self.encoder.transform(X)
        common = dict(random_state=SEED, n_jobs=self.n_jobs, verbose=-1, deterministic=True, force_row_wise=True,
                      objective="quantile", **self.hyperparams)
        self.lo_model = LGBMRegressor(alpha=self.alpha / 2, **common).fit(Xt, y)
        self.hi_model = LGBMRegressor(alpha=1 - self.alpha / 2, **common).fit(Xt, y)
        for m in (self.lo_model, self.hi_model):
            m.set_params(n_jobs=1)

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        if self.encoder is None:
            raise RuntimeError("fit() first")
        rows = feature_rows(history, future)
        mean = self.point.predict_rows(rows)
        Xt = self.encoder.transform(rows)
        anchor = rows["anchor_pace"].to_numpy(dtype=float)
        lo = anchor + np.asarray(self.lo_model.predict(Xt), dtype=float)
        hi = anchor + np.asarray(self.hi_model.predict(Xt), dtype=float)
        lo, hi = order_bounds(mean, np.minimum(lo, hi), np.maximum(lo, hi))
        return Forecast(mean=mean, lo=lo, hi=hi)
