"""Nested hyperparameter search for the tree forecasters.

TunedForecaster.fit(train) runs the whole search INSIDE the training races it is given: seeded
random sampling of ≤ n_trials configurations, each scored by GroupKFold-by-race CV on that
training set's forecast rows (MAE of the predicted lap time, i.e. of lap − anchor), then the best
configuration is refitted on all of the training races. When the evaluation harness calls fit()
per outer fold, the outer test race is never seen by its own tuning: the search is nested by
construction. Every trial is kept in .trials for logging.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from .config import SEED
from .features import FoldEncoder, grouped_folds
from .models.forecasting import Forecast
from .models.tree_models import FACTORIES, TreeForecaster, fold_rows

SEARCH_SPACES: dict[str, dict[str, list[Any]]] = {
    "lightgbm": {
        "n_estimators": [100, 300, 600], "learning_rate": [0.02, 0.05, 0.1], "num_leaves": [15, 31, 63, 127],
        "min_child_samples": [20, 50, 100, 200], "subsample": [0.7, 1.0], "subsample_freq": [1],
        "colsample_bytree": [0.7, 1.0], "reg_lambda": [0.0, 1.0, 5.0],
    },
    "xgboost": {
        "n_estimators": [100, 300, 600], "learning_rate": [0.02, 0.05, 0.1], "max_depth": [3, 4, 6, 8],
        "min_child_weight": [1, 5, 20], "subsample": [0.7, 1.0], "colsample_bytree": [0.7, 1.0], "reg_lambda": [1.0, 5.0],
    },
}


def sample_configs(kind: str, n: int, seed: int = SEED) -> list[dict[str, Any]]:
    """n distinct configurations, seeded; the library defaults are always trial 0."""
    space = SEARCH_SPACES[kind]
    rng = np.random.default_rng(seed)
    configs: list[dict[str, Any]] = [{}]
    seen = {"{}"}
    attempts = 0
    while len(configs) < n and attempts < 50 * n:
        attempts += 1
        cfg = {k: v[int(rng.integers(len(v)))] for k, v in space.items()}
        key = repr(sorted(cfg.items()))
        if key not in seen:
            seen.add(key)
            configs.append(cfg)
    return configs


class TunedForecaster:
    def __init__(self, kind: str, *, identity: bool = False, n_trials: int = 20, inner_folds: int = 3,
                 k_step: int = 2, n_jobs: int = 1) -> None:
        if kind not in SEARCH_SPACES:
            raise ValueError(f"no search space for {kind!r}")
        self.kind, self.identity, self.n_trials, self.inner_folds = kind, identity, n_trials, inner_folds
        self.k_step, self.n_jobs = k_step, n_jobs
        self.name = f"{kind}{'+identity' if identity else ''}+tuned"
        self.best: dict[str, Any] = {}
        self.trials: list[dict[str, Any]] = []
        self.model: TreeForecaster | None = None

    def params(self) -> dict[str, object]:
        return {"kind": self.kind, "identity": self.identity, "n_trials": self.n_trials,
                "inner_cv": f"GroupKFold({self.inner_folds}) by race", "search": "seeded random"}

    def _inner_mae(self, X: pd.DataFrame, y: npt.NDArray[np.float64], groups: npt.NDArray[Any], cfg: dict[str, Any]) -> float:
        native = FACTORIES[self.kind][1]
        scores = []
        for tr, va in grouped_folds(groups, self.inner_folds):
            enc = FoldEncoder(identity=self.identity, native_categorical=native).fit(X.iloc[tr])
            est = FACTORIES[self.kind][0](n_jobs=self.n_jobs, **cfg)
            est.fit(enc.transform(X.iloc[tr]), y[tr])
            pred = np.asarray(est.predict(enc.transform(X.iloc[va])), dtype=float)
            scores.append(float(np.mean(np.abs(pred - y[va]))))
        return float(np.mean(scores))

    def fit(self, train: pd.DataFrame) -> None:
        X, y, groups = fold_rows(train, self.k_step)
        self.trials = []
        for i, cfg in enumerate(sample_configs(self.kind, self.n_trials)):
            self.trials.append({"trial": i, "inner_mae": self._inner_mae(X, y, groups, cfg), **cfg})
        best = min(self.trials, key=lambda t: (t["inner_mae"], t["trial"]))
        self.best = {k: v for k, v in best.items() if k not in ("trial", "inner_mae")}
        self.model = TreeForecaster(self.kind, identity=self.identity, k_step=self.k_step, n_jobs=self.n_jobs,
                                    hyperparams=self.best)
        self.model.fit(train)

    def predict_rows(self, rows: pd.DataFrame) -> npt.NDArray[np.float64]:
        if self.model is None:
            raise RuntimeError("fit() first")
        return self.model.predict_rows(rows)

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        if self.model is None:
            raise RuntimeError("fit() first")
        return self.model.forecast(history, future)
