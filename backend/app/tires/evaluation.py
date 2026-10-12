"""Phase 6C evaluation harness: splits, forecast simulation, metrics and race-level aggregation.

Splits are never random. split_generator() is the ONLY way data reaches a model:
  loro          leave-one-race-out: train on every other race, test on the held-out one
  walk_forward  races ordered by (year, round); each race after the first min_train is tested
                with every earlier race as training data

Forecast simulation (what the product does): for each test (driver, stint) with ≥ 12 clean laps
and each cut k = 6, 8, …, n − h, the model sees the stint's first k clean laps (history) and
the next h laps' KNOWN context (future), and predicts their lap times. forecast_inputs() enforces
the information boundary: future lap times and prev_lap_time_s are masked, weather and position
are frozen at lap k, and only deterministic columns (lap number, tyre age, fuel_proxy from the
known race length, compound, stint) pass through.

Aggregation is the mean over RACES (with the standard error across races), never pooled laps, so
long races do not dominate.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from .models.forecasting import ForecastModel

SplitKind = Literal["loro", "walk_forward"]
FloatArray = npt.NDArray[np.float64]

MIN_STINT_LAPS = 12
FIRST_CUT = 6
CUT_STEP = 2
SLOPE_REFERENCE_MIN_LAPS = 15
INTERVAL_ALPHA = 0.05  # benchmark intervals are 95%

# Columns whose future values are unknown at the cut and how they are handled.
MASKED = ("lap_time_s", "prev_lap_time_s")
FROZEN = ("track_temp_c", "air_temp_c", "rainfall", "position")


# ---------------------------------------------------------------- splits
@dataclass(frozen=True, eq=False)
class Fold:
    kind: SplitKind
    test_key: str
    train_keys: tuple[str, ...]
    train: pd.DataFrame
    test: pd.DataFrame


def race_order(frame: pd.DataFrame) -> list[str]:
    """Race keys in date order. (year, round) is the calendar order; the dataset has no date column."""
    races = frame[["session_key", "year", "round"]].drop_duplicates("session_key")
    return races.sort_values(["year", "round", "session_key"])["session_key"].astype(str).tolist()


def split_generator(frame: pd.DataFrame, kind: str, min_train: int = 8) -> Iterator[Fold]:
    order = race_order(frame)
    if kind == "loro":
        pairs = [(key, [k for k in order if k != key]) for key in order]
    elif kind == "walk_forward":
        pairs = [(key, order[:i]) for i, key in enumerate(order) if i >= min_train]
    else:
        raise ValueError(f"unknown split kind {kind!r}; use 'loro' or 'walk_forward'")
    keys = frame["session_key"].astype(str)
    for test_key, train_keys in pairs:
        yield Fold(kind=kind, test_key=test_key, train_keys=tuple(train_keys),  # type: ignore[arg-type]
                   train=frame[keys.isin(train_keys)], test=frame[keys == test_key])


# ---------------------------------------------------------------- the information boundary
def forecast_inputs(stint: pd.DataFrame, k: int, h: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(history = first k clean laps, future = next h laps with unknowable columns masked)."""
    s = stint.sort_values("lap_number").reset_index(drop=True)
    history = s.iloc[:k].copy()
    future = s.iloc[k:k + h].copy()
    last = history.iloc[-1]
    for col in MASKED:
        future[col] = np.nan
    for col in FROZEN:
        future[col] = last[col]
    future["laps_since_neutralization"] = (
        int(last["laps_since_neutralization"]) + (future["lap_number"] - int(last["lap_number"]))
    )  # assumes no new neutralization: the only thing knowable at lap k
    return history, future


# ---------------------------------------------------------------- simulation
RECORD_COLUMNS = ("session_key", "circuit", "driver", "stint", "compound", "k", "horizon", "step",
                  "lap_number", "tyre_age", "actual", "pred", "lo", "hi")


def simulate_race(model: ForecastModel, test: pd.DataFrame, horizons: Sequence[int] = (5, 10)) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for (driver, stint_no), stint in test.groupby(["driver", "stint"], sort=True):
        stint = stint.sort_values("lap_number").reset_index(drop=True)
        n = len(stint)
        if n < MIN_STINT_LAPS:
            continue
        for h in horizons:
            for k in range(FIRST_CUT, n - h + 1, CUT_STEP):
                history, future = forecast_inputs(stint, k, h)
                fc = model.forecast(history, future)
                actual = stint["lap_time_s"].iloc[k:k + h].to_numpy(dtype=float)
                for step in range(h):
                    rows.append({
                        "session_key": stint["session_key"].iloc[0], "circuit": stint["circuit"].iloc[0],
                        "driver": driver, "stint": int(stint_no), "compound": stint["compound"].iloc[0],
                        "k": k, "horizon": h, "step": step + 1,
                        "lap_number": int(future["lap_number"].iloc[step]), "tyre_age": int(future["tyre_age"].iloc[step]),
                        "actual": float(actual[step]), "pred": float(fc.mean[step]),
                        "lo": float(fc.lo[step]) if fc.lo is not None else np.nan,
                        "hi": float(fc.hi[step]) if fc.hi is not None else np.nan,
                    })
    return pd.DataFrame(rows, columns=list(RECORD_COLUMNS))


def ex_post_slope(stint: pd.DataFrame) -> float:
    """OLS slope of lap time on tyre age over the whole stint: a REFERENCE, not ground truth
    (it includes fuel burn-off and everything else that changed during the stint)."""
    age = stint["tyre_age"].to_numpy(dtype=float)
    y = stint["lap_time_s"].to_numpy(dtype=float)
    return float(np.polyfit(age, y, 1)[0])


def slope_errors(model: ForecastModel, test: pd.DataFrame) -> pd.DataFrame:
    """For stints ≥ 15 laps: forecast every lap after the first cut, and compare the forecast's
    lap-time-vs-age slope with the stint's own ex-post slope."""
    rows: list[dict[str, object]] = []
    for (driver, stint_no), stint in test.groupby(["driver", "stint"], sort=True):
        stint = stint.sort_values("lap_number").reset_index(drop=True)
        n = len(stint)
        if n < SLOPE_REFERENCE_MIN_LAPS:
            continue
        history, future = forecast_inputs(stint, FIRST_CUT, n - FIRST_CUT)
        pred = model.forecast(history, future).mean
        model_slope = float(np.polyfit(future["tyre_age"].to_numpy(dtype=float), pred, 1)[0])
        ref = ex_post_slope(stint)
        rows.append({"session_key": stint["session_key"].iloc[0], "driver": driver, "stint": int(stint_no),
                     "compound": stint["compound"].iloc[0], "n": n, "reference": ref, "model_slope": model_slope,
                     "error": model_slope - ref})
    return pd.DataFrame(rows, columns=["session_key", "driver", "stint", "compound", "n", "reference", "model_slope", "error"])


# ---------------------------------------------------------------- metrics (pure)
def _a(values: "pd.Series[Any]") -> FloatArray:
    return values.to_numpy(dtype=float)


def mae(pred: FloatArray, actual: FloatArray) -> float:
    return float(np.mean(np.abs(np.asarray(pred) - np.asarray(actual))))


def rmse(pred: FloatArray, actual: FloatArray) -> float:
    return float(np.sqrt(np.mean((np.asarray(pred) - np.asarray(actual)) ** 2)))


def bias(pred: FloatArray, actual: FloatArray) -> float:
    """Mean signed error, pred − actual: > 0 means forecasts are too slow."""
    return float(np.mean(np.asarray(pred) - np.asarray(actual)))


def coverage(lo: FloatArray, hi: FloatArray, y: FloatArray) -> float:
    y = np.asarray(y)
    return float(np.mean((np.asarray(lo) <= y) & (y <= np.asarray(hi))))


def mean_width(lo: FloatArray, hi: FloatArray) -> float:
    return float(np.mean(np.asarray(hi) - np.asarray(lo)))


def interval_score(lo: FloatArray, hi: FloatArray, y: FloatArray, alpha: float) -> float:
    """Gneiting & Raftery (2007): (u − l) + (2/α)(l − y)·1[y < l] + (2/α)(y − u)·1[y > u]. Lower is better."""
    lo, hi, y = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float), np.asarray(y, dtype=float)
    score = (hi - lo) + (2 / alpha) * (lo - y) * (y < lo) + (2 / alpha) * (y - hi) * (y > hi)
    return float(np.mean(score))


@dataclass(frozen=True)
class Aggregate:
    mean: float
    se: float
    n: int
    per_race: dict[str, float] = field(default_factory=dict)


def aggregate(records: pd.DataFrame, metric: Callable[[FloatArray, FloatArray], float]) -> Aggregate:
    """metric(pred, actual) per race, then the mean over races ± standard error across races."""
    return aggregate_frame(records, lambda d: metric(d["pred"].to_numpy(dtype=float), d["actual"].to_numpy(dtype=float)))


def aggregate_frame(records: pd.DataFrame, fn: Callable[[pd.DataFrame], float]) -> Aggregate:
    per_race = {str(k): float(fn(g)) for k, g in records.groupby("session_key", sort=True)} if len(records) else {}
    vals = np.array(list(per_race.values()), dtype=float)
    vals = vals[np.isfinite(vals)]
    if not len(vals):
        return Aggregate(math.nan, math.nan, 0, per_race)
    se = float(np.std(vals, ddof=1) / np.sqrt(len(vals))) if len(vals) > 1 else math.nan
    return Aggregate(float(np.mean(vals)), se, len(vals), per_race)


# ---------------------------------------------------------------- end to end
@dataclass
class EvalResult:
    summary: pd.DataFrame        # one row per (model, split, horizon)
    records: pd.DataFrame        # every forecast lap
    slopes: pd.DataFrame         # slope-vs-reference rows
    by_step: pd.DataFrame        # MAE by horizon step (mean over races)
    by_compound: pd.DataFrame
    by_race: pd.DataFrame


def evaluate(
    frame: pd.DataFrame,
    models: Sequence[ForecastModel],
    *,
    splits: Sequence[str] = ("loro", "walk_forward"),
    horizons: Sequence[int] = (5, 10),
    min_train: int = 8,
) -> EvalResult:
    rec_parts: list[pd.DataFrame] = []
    slope_parts: list[pd.DataFrame] = []
    for split in splits:
        for fold in split_generator(frame, split, min_train=min_train):
            for model in models:
                model.fit(fold.train)
                r = simulate_race(model, fold.test, horizons)
                r.insert(0, "model", model.name)
                r.insert(1, "split", split)
                rec_parts.append(r)
                s = slope_errors(model, fold.test)
                s.insert(0, "model", model.name)
                s.insert(1, "split", split)
                slope_parts.append(s)
    records = pd.concat(rec_parts, ignore_index=True)
    slopes = pd.concat(slope_parts, ignore_index=True)

    summary_rows, step_rows, comp_rows, race_rows = [], [], [], []
    for (model, split, h), g in records.groupby(["model", "split", "horizon"], sort=True):
        m, rm, b = aggregate(g, mae), aggregate(g, rmse), aggregate(g, bias)
        hz = int(cast(int, h))
        row: dict[str, object] = {"model": model, "split": split, "horizon": hz, "n_races": m.n,
                                  "n_forecast_laps": len(g), "mae": m.mean, "mae_se": m.se, "rmse": rm.mean,
                                  "bias": b.mean, "bias_se": b.se}
        has_interval = g["lo"].notna().any()
        row["coverage"] = aggregate_frame(g, lambda d: coverage(_a(d["lo"]), _a(d["hi"]), _a(d["actual"]))).mean if has_interval else math.nan
        row["mean_width"] = aggregate_frame(g, lambda d: mean_width(_a(d["lo"]), _a(d["hi"]))).mean if has_interval else math.nan
        row["interval_score"] = (aggregate_frame(g, lambda d: interval_score(_a(d["lo"]), _a(d["hi"]), _a(d["actual"]), INTERVAL_ALPHA)).mean
                                 if has_interval else math.nan)
        sl = slopes[(slopes["model"] == model) & (slopes["split"] == split)]
        sa = aggregate_frame(sl, lambda d: float(np.mean(np.abs(d["error"])))) if len(sl) else Aggregate(math.nan, math.nan, 0)
        row["slope_mae_vs_ref"] = sa.mean
        summary_rows.append(row)
        for step, gs in g.groupby("step", sort=True):
            step_rows.append({"model": model, "split": split, "horizon": hz, "step": int(cast(int, step)), "mae": aggregate(gs, mae).mean})
        for comp, gc in g.groupby("compound", sort=True):
            comp_rows.append({"model": model, "split": split, "horizon": hz, "compound": comp,
                              "mae": aggregate(gc, mae).mean, "n_races": aggregate(gc, mae).n})
        for race, value in m.per_race.items():
            race_rows.append({"model": model, "split": split, "horizon": hz, "session_key": race, "mae": value})
    return EvalResult(summary=pd.DataFrame(summary_rows), records=records, slopes=slopes,
                      by_step=pd.DataFrame(step_rows), by_compound=pd.DataFrame(comp_rows), by_race=pd.DataFrame(race_rows))
