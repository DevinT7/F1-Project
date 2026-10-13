"""Servable tyre models (Phase 6E): coefficients in JSON, calibration residuals in .npz.

No pickles: an artifact is data, never code, so loading one cannot execute anything, and it
does not depend on statsmodels/patsy internals (the 6D pickle of Baseline B was 119 MB and
could only be unpickled from inside a function).

    kind "pooled_fixed_effects_recent"  Baseline B anchored on the last 3 laps (6E production)
    kind "pooled_fixed_effects"   Baseline B: stint level from its history, then
                                  β_fuel·fuel_proxy + β_age,c·tyre_age (+ γ_c·age²) + β_temp·track_temp
    kind "persistence"            mean of the last 3 history laps, flat

Intervals: split-conformal, prediction ± the finite-sample (1 − α) quantile of the calibration
residuals for that horizon step. Steps beyond the calibrated range use the largest calibrated
quantile and the caller is told (extrapolated_steps).
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

from .models.intervals import conformal_quantile

FloatArray = npt.NDArray[np.float64]
FORMAT = "raceiq-tire-model/1"
ANCHOR_LAPS = 3


@dataclass(frozen=True)
class Degradation:
    value_s_per_lap: float
    ci_low: float | None
    ci_high: float | None
    method: str


@dataclass(frozen=True)
class ServedForecast:
    mean: FloatArray
    lo: FloatArray
    hi: FloatArray
    extrapolated_steps: tuple[int, ...] = ()


class PointModel(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def fuel_coef_s_per_lap(self) -> float | None: ...   # None: the model has no explicit fuel term

    def point(self, history: pd.DataFrame, future: pd.DataFrame) -> FloatArray: ...
    def degradation(self, compound: str, tyre_age: float) -> Degradation | None: ...
    def supports(self, compound: str) -> bool: ...


@dataclass(frozen=True)
class FixedEffectsPoint:
    beta_fuel: float
    beta_age: dict[str, float]
    gamma: dict[str, float]
    beta_temp: float
    degradation_ci: dict[str, tuple[float, float]]   # per compound, from the fit (delta method)
    anchor_laps: int | None = None                   # None: level from all history laps (6D Baseline B)

    @property
    def kind(self) -> str:
        return "pooled_fixed_effects" if self.anchor_laps is None else "pooled_fixed_effects_recent"

    @property
    def fuel_coef_s_per_lap(self) -> float | None:
        return self.beta_fuel

    def supports(self, compound: str) -> bool:
        return compound in self.beta_age and math.isfinite(self.beta_age[compound])

    def _fixed(self, df: pd.DataFrame, compound: str) -> FloatArray:
        age = df["tyre_age"].to_numpy(dtype=float)
        out: FloatArray = (self.beta_fuel * df["fuel_proxy"].to_numpy(dtype=float) + self.beta_age[compound] * age
                           + self.gamma.get(compound, 0.0) * age ** 2
                           + self.beta_temp * df["track_temp_c"].to_numpy(dtype=float))
        return out

    def point(self, history: pd.DataFrame, future: pd.DataFrame) -> FloatArray:
        compound = str(history["compound"].iloc[0])
        h = history.sort_values("lap_number")
        if self.anchor_laps is not None:
            h = h.iloc[-self.anchor_laps:]
        level = float(np.mean(h["lap_time_s"].to_numpy(dtype=float) - self._fixed(h, compound)))
        return np.asarray(level + self._fixed(future, compound), dtype=np.float64)

    def degradation(self, compound: str, tyre_age: float) -> Degradation | None:
        if not self.supports(compound):
            return None
        value = self.beta_age[compound] + 2.0 * self.gamma.get(compound, 0.0) * tyre_age
        lo, hi = self.degradation_ci.get(compound, (math.nan, math.nan))
        ok = math.isfinite(lo) and math.isfinite(hi) and self.gamma.get(compound, 0.0) == 0.0
        return Degradation(value, lo if ok else None, hi if ok else None, "baseline_b_pooled_fixed_effects")


@dataclass(frozen=True)
class PersistencePoint:
    kind: str = "persistence"
    fuel_coef_s_per_lap: float | None = None

    def supports(self, compound: str) -> bool:
        return True

    def point(self, history: pd.DataFrame, future: pd.DataFrame) -> FloatArray:
        level = float(np.mean(history["lap_time_s"].to_numpy(dtype=float)[-ANCHOR_LAPS:]))
        return np.full(len(future), level, dtype=np.float64)

    def degradation(self, compound: str, tyre_age: float) -> Degradation | None:
        return None


@dataclass(frozen=True)
class ServedModel:
    point_model: PointModel
    alpha: float
    residuals: dict[int, FloatArray]
    step_quantiles: dict[int, float] = field(default_factory=dict)

    @classmethod
    def build(cls, point_model: PointModel, alpha: float, residuals: dict[int, FloatArray]) -> ServedModel:
        quantiles = {s: conformal_quantile(r, alpha) for s, r in residuals.items()}
        return cls(point_model, alpha, residuals, quantiles)

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> ServedForecast:
        mean = self.point_model.point(history, future)
        top = max(self.step_quantiles)
        q = np.array([self.step_quantiles.get(i + 1, self.step_quantiles[top]) for i in range(len(mean))], dtype=float)
        extrapolated = tuple(i + 1 for i in range(len(mean)) if i + 1 not in self.step_quantiles)
        return ServedForecast(mean, mean - q, mean + q, extrapolated)


# ---------------------------------------------------------------- (de)serialization
def point_model_to_json(model: PointModel) -> dict[str, Any]:
    if isinstance(model, FixedEffectsPoint):
        return {"kind": model.kind, "beta_fuel": model.beta_fuel, "beta_age": model.beta_age, "gamma": model.gamma,
                "beta_temp": model.beta_temp, "degradation_ci": {c: list(v) for c, v in model.degradation_ci.items()},
                "anchor_laps": model.anchor_laps}
    if isinstance(model, PersistencePoint):
        return {"kind": model.kind, "anchor_laps": ANCHOR_LAPS}
    raise TypeError(f"not a servable point model: {type(model).__name__}")


def point_model_from_json(data: dict[str, Any]) -> PointModel:
    kind = data.get("kind")
    if kind in ("pooled_fixed_effects", "pooled_fixed_effects_recent"):
        return FixedEffectsPoint(
            beta_fuel=float(data["beta_fuel"]), beta_age={k: float(v) for k, v in data["beta_age"].items()},
            gamma={k: float(v) for k, v in data["gamma"].items()}, beta_temp=float(data["beta_temp"]),
            degradation_ci={k: (float(v[0]), float(v[1])) for k, v in data["degradation_ci"].items()},
            anchor_laps=None if data.get("anchor_laps") is None else int(data["anchor_laps"]))
    if kind == "persistence":
        return PersistencePoint()
    raise ValueError(f"unknown servable model kind {kind!r}")


def residuals_to_npz(residuals: dict[int, FloatArray]) -> bytes:
    buf = io.BytesIO()
    arrays: dict[str, Any] = {f"step_{s}": np.asarray(r, dtype=np.float64) for s, r in sorted(residuals.items())}
    np.savez_compressed(buf, **arrays)
    return buf.getvalue()


def residuals_from_npz(data: bytes) -> dict[int, FloatArray]:
    with np.load(io.BytesIO(data), allow_pickle=False) as npz:
        return {int(name.split("_", 1)[1]): np.asarray(npz[name], dtype=np.float64) for name in npz.files}
