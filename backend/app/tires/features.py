"""Phase 6D feature pipeline: fold-safe, train/serve-identical, information-boundary respecting.

Target (documented in docs/tire_model_report.md): the lap time MINUS the stint's recent pace,
where recent pace ("anchor") = mean of the last ANCHOR_LAPS clean laps before the forecast cut.
Trees cannot extrapolate absolute lap-time levels across circuits (~70–110 s), and persistence
(= the anchor) is the benchmark to beat, so the model learns the correction to persistence.
The anchor uses only laps available at the cut.

One core function builds both the training rows (every cut of every training stint) and the
forecast-time rows, so there is no train/serve skew. Columns are only what is knowable at the cut:
  - deterministic future facts: tyre_age, fuel_proxy (race length is known), steps_ahead
  - projections from the cut: stint_lap_index (index at cut + steps; the real future index would
    reveal which upcoming laps get excluded), laps_since_neutralization (assumes no new SC)
  - weather frozen at the cut (track_temp_c, air_temp_c)
  - context: compound (ordinal SOFT 0 < MEDIUM 1 < HARD 2 by nominal hardness), circuit, year,
    and, in the identity variant only, driver and team

FoldEncoder learns categories from the training fold ONLY; unseen categories become "unknown"
(all-zero one-hot / NaN native category).
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from sklearn.model_selection import GroupKFold

ANCHOR_LAPS = 3
COMPOUND_ORD: dict[str, int] = {"SOFT": 0, "MEDIUM": 1, "HARD": 2}
NUMERIC_FEATURES: tuple[str, ...] = (
    "tyre_age", "age_at_cut", "steps_ahead", "age_gain", "fuel_proxy", "fuel_drop", "compound_ord",
    "track_temp_c", "air_temp_c", "laps_since_neutralization", "stint_lap_index", "year",
)
BASE_CATEGORICAL: tuple[str, ...] = ("circuit",)
IDENTITY_CATEGORICAL: tuple[str, ...] = ("driver", "team")
ROW_COLUMNS: tuple[str, ...] = (*NUMERIC_FEATURES, *BASE_CATEGORICAL, *IDENTITY_CATEGORICAL, "stint", "anchor_pace")


def _core(hist: dict[str, npt.NDArray[Any]], fut: dict[str, npt.NDArray[Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Feature columns for the future laps given history arrays (all knowable at the cut)."""
    tail = slice(-ANCHOR_LAPS, None)
    m = len(fut["tyre_age"])
    anchor_age = float(np.mean(hist["tyre_age"][tail]))
    anchor_fuel = float(np.mean(hist["fuel_proxy"][tail]))
    steps = np.arange(1, m + 1)
    return {
        "tyre_age": fut["tyre_age"].astype(float),
        "age_at_cut": np.full(m, float(hist["tyre_age"][-1])),
        "steps_ahead": steps,
        "age_gain": fut["tyre_age"].astype(float) - anchor_age,
        "fuel_proxy": fut["fuel_proxy"].astype(float),
        "fuel_drop": anchor_fuel - fut["fuel_proxy"].astype(float),
        "compound_ord": np.full(m, COMPOUND_ORD.get(meta["compound"], 1)),
        "track_temp_c": np.full(m, float(hist["track_temp_c"][-1])),
        "air_temp_c": np.full(m, float(hist["air_temp_c"][-1])),
        "laps_since_neutralization": float(hist["laps_since_neutralization"][-1]) + (fut["lap_number"] - hist["lap_number"][-1]),
        "stint_lap_index": float(hist["stint_lap_index"][-1]) + steps,
        "year": np.full(m, int(meta["year"])),
        "circuit": [meta["circuit"]] * m,
        "driver": [meta["driver"]] * m,
        "team": [meta["team"]] * m,
        "stint": np.full(m, int(meta["stint"])),
        "anchor_pace": np.full(m, float(np.mean(hist["lap_time_s"][tail]))),
    }


_ARRAYS = ("lap_time_s", "tyre_age", "fuel_proxy", "track_temp_c", "air_temp_c", "laps_since_neutralization",
           "lap_number", "stint_lap_index")


def _arrays(df: pd.DataFrame) -> dict[str, npt.NDArray[Any]]:
    return {c: df[c].to_numpy(dtype=float) for c in _ARRAYS}


def _meta(df: pd.DataFrame) -> dict[str, Any]:
    first = df.iloc[0]
    return {k: first[k] for k in ("compound", "year", "circuit", "driver", "team", "stint")}


def feature_rows(history: pd.DataFrame, future: pd.DataFrame) -> pd.DataFrame:
    """Forecast-time rows: one per future lap (future comes from evaluation.forecast_inputs)."""
    h = history.sort_values("lap_number")
    rows = _core(_arrays(h), _arrays(future), _meta(h))
    return pd.DataFrame(rows, columns=list(ROW_COLUMNS))


def training_rows(frame: pd.DataFrame, *, k_step: int = 1, max_steps: int = 10) -> tuple[pd.DataFrame, npt.NDArray[np.float64], npt.NDArray[Any]]:
    """Every cut k ≥ ANCHOR_LAPS of every training stint, up to max_steps laps ahead.
    Returns (rows with a 'cut' column, target = lap time − anchor pace, race key per row)."""
    parts: list[pd.DataFrame] = []
    targets: list[npt.NDArray[np.float64]] = []
    groups: list[npt.NDArray[Any]] = []
    for (key, _driver, _stint), s in frame.groupby(["session_key", "driver", "stint"], sort=True):
        s = s.sort_values("lap_number")
        n = len(s)
        if n <= ANCHOR_LAPS:
            continue
        arr = _arrays(s)
        meta = _meta(s)
        for k in range(ANCHOR_LAPS, n, k_step):
            hist = {c: v[:k] for c, v in arr.items()}
            fut = {c: v[k:k + max_steps] for c, v in arr.items()}
            rows = _core(hist, fut, meta)
            rows["cut"] = np.full(len(fut["tyre_age"]), k)
            parts.append(pd.DataFrame(rows))
            targets.append(np.asarray(fut["lap_time_s"] - float(np.mean(hist["lap_time_s"][-ANCHOR_LAPS:])), dtype=np.float64))
            groups.append(np.full(len(fut["tyre_age"]), str(key), dtype=object))
    if not parts:
        return pd.DataFrame(columns=[*ROW_COLUMNS, "cut"]), np.zeros(0), np.zeros(0, dtype=object)
    X = pd.concat(parts, ignore_index=True)[[*ROW_COLUMNS, "cut"]]
    return X, np.concatenate(targets), np.concatenate(groups)


class FoldEncoder:
    """Numeric features as floats + categoricals (circuit; driver/team when identity=True), with
    categories learned from the training fold ONLY. One-hot by default; pandas categoricals with
    fixed categories for models with native categorical support (LightGBM)."""

    def __init__(self, *, identity: bool, native_categorical: bool = False) -> None:
        self.identity = identity
        self.native_categorical = native_categorical
        self.categoricals = BASE_CATEGORICAL + (IDENTITY_CATEGORICAL if identity else ())
        self.categories_: dict[str, list[str]] = {}
        self.columns_: list[str] = []

    def fit(self, X: pd.DataFrame) -> FoldEncoder:
        self.categories_ = {c: sorted(str(v) for v in X[c].dropna().unique()) for c in self.categoricals}
        if self.native_categorical:
            self.columns_ = [*NUMERIC_FEATURES, *self.categoricals]
        else:
            self.columns_ = [*NUMERIC_FEATURES, *(f"{c}={v}" for c in self.categoricals for v in self.categories_[c])]
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame({c: X[c].to_numpy(dtype=float) for c in NUMERIC_FEATURES}, index=X.index)
        for c in self.categoricals:
            values = X[c].astype(str)
            if self.native_categorical:
                out[c] = pd.Categorical(values.where(values.isin(self.categories_[c])), categories=self.categories_[c])
            else:
                for v in self.categories_[c]:
                    out[f"{c}={v}"] = (values == v).to_numpy(dtype=float)
        return out[self.columns_]


def grouped_folds(groups: npt.NDArray[Any], n_splits: int = 5) -> Iterator[tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]]:
    """Inner CV for tuning: GroupKFold by race, so a race is never split across folds."""
    n = min(n_splits, len(set(groups)))
    yield from GroupKFold(n_splits=n).split(np.zeros(len(groups)), groups=groups)
