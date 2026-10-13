"""Phase 6D tree forecasters: RandomForest, GradientBoosting (sklearn), XGBoost, LightGBM.

Each predicts lap time − recent pace (features.py) and adds the anchor back. fit() sees only the
training races (rows from features.training_rows); forecast() sees only history + masked future,
so the 6C information boundary holds (tested for every model × variant).

"GradientBoosting (sklearn)" is HistGradientBoostingRegressor: sklearn's own recommendation for
datasets of this size (~100k rows per fold); the classic GradientBoostingRegressor would take
minutes per fold. Two variants per model: without (default) and with driver/team identity features.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from xgboost import XGBRegressor

from ..config import SEED
from ..features import FoldEncoder, feature_rows, training_rows
from .forecasting import Forecast

Estimator = Any
Rows = tuple[pd.DataFrame, npt.NDArray[np.float64], npt.NDArray[Any]]
Factory = Callable[..., Estimator]

FACTORIES: dict[str, tuple[Factory, bool]] = {  # name → (factory(**params), native categorical support)
    "random_forest": (lambda n_jobs=1, **p: RandomForestRegressor(random_state=SEED, n_jobs=n_jobs, **p), False),
    "gradient_boosting": (lambda n_jobs=1, **p: HistGradientBoostingRegressor(random_state=SEED, **p), False),
    "xgboost": (lambda n_jobs=1, **p: XGBRegressor(random_state=SEED, n_jobs=n_jobs, tree_method="hist", **p), False),
    "lightgbm": (lambda n_jobs=1, **p: LGBMRegressor(random_state=SEED, n_jobs=n_jobs, verbose=-1, deterministic=True,
                                                     force_row_wise=True, **p), True),
}

_ROWS_CACHE: dict[tuple[str, int, float, int], Rows] = {}


def race_rows(race: pd.DataFrame, k_step: int) -> Rows:
    """Training rows of ONE race, cached (rows depend only on that race, never on the fold)."""
    key = (str(race["session_key"].iloc[0]), len(race), float(race["lap_time_s"].sum()), k_step)
    if key not in _ROWS_CACHE:
        _ROWS_CACHE[key] = training_rows(race, k_step=k_step)
    return _ROWS_CACHE[key]


def fold_rows(train: pd.DataFrame, k_step: int) -> Rows:
    parts = [race_rows(g, k_step) for _, g in train.groupby("session_key", sort=True)]
    return (pd.concat([p[0] for p in parts], ignore_index=True), np.concatenate([p[1] for p in parts]),
            np.concatenate([p[2] for p in parts]))


class TreeForecaster:
    def __init__(self, kind: str, *, identity: bool = False, k_step: int = 2, n_jobs: int = 1,
                 hyperparams: dict[str, Any] | None = None, monotone_age: bool = False) -> None:
        if kind not in FACTORIES:
            raise ValueError(f"unknown model {kind!r}")
        self.kind = kind
        self.identity = identity
        self.k_step = k_step
        self.n_jobs = n_jobs
        self.hyperparams = dict(hyperparams or {})
        self.monotone_age = monotone_age
        self.name = kind + ("+identity" if identity else "") + ("+monotone" if monotone_age else "")
        self.native = FACTORIES[kind][1]
        self.encoder: FoldEncoder | None = None
        self.model: Estimator | None = None

    def params(self) -> dict[str, object]:
        return {"kind": self.kind, "identity": self.identity, "k_step": self.k_step,
                "monotone_age": self.monotone_age, **self.hyperparams}

    def _estimator(self) -> Estimator:
        params = dict(self.hyperparams)
        if self.monotone_age:
            assert self.encoder is not None
            cons = [1 if c in ("tyre_age", "age_gain") else 0 for c in self.encoder.columns_]
            params["monotone_constraints"] = cons if self.kind == "lightgbm" else "(" + ",".join(map(str, cons)) + ")"
        return FACTORIES[self.kind][0](n_jobs=self.n_jobs, **params)

    def fit(self, train: pd.DataFrame) -> None:
        X, y, _ = fold_rows(train, self.k_step)
        self.encoder = FoldEncoder(identity=self.identity, native_categorical=self.native).fit(X)
        self.model = self._estimator()
        self.model.fit(self.encoder.transform(X), y)
        if hasattr(self.model, "n_jobs"):
            self.model.set_params(n_jobs=1)  # forecasts are tiny batches; thread start-up would dominate

    def predict_rows(self, rows: pd.DataFrame) -> npt.NDArray[np.float64]:
        if self.model is None or self.encoder is None:
            raise RuntimeError("fit() first")
        delta = np.asarray(self.model.predict(self.encoder.transform(rows)), dtype=np.float64)
        out = np.asarray(rows["anchor_pace"].to_numpy(dtype=np.float64) + delta, dtype=np.float64)
        return out

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        return Forecast(mean=self.predict_rows(feature_rows(history, future)))


def make_tree_forecaster(kind: str, *, identity: bool = False, **kwargs: Any) -> TreeForecaster:
    return TreeForecaster(kind, identity=identity, **kwargs)
