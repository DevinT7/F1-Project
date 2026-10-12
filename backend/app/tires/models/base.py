"""The interface every Phase 6 degradation model implements.

"Degradation" is always a COMBINED ADJUSTED TREND (tire wear + residual track evolution after
the fuel adjustment), never pure tire wear. Missing estimates are None, never 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, eq=False)
class StintContext:
    """The stint a degradation estimate is requested for."""

    session_key: str
    driver: str
    stint: int
    compound: str
    tyre_age: float          # age (laps) at which to evaluate the degradation rate
    laps: pd.DataFrame       # the stint's clean laps (6A dataset rows)


@dataclass(frozen=True)
class DegradationEstimate:
    value_s_per_lap: float | None   # d(lap time)/d(tyre age) at ctx.tyre_age; None = no honest estimate
    ci_low: float | None            # 95% interval
    ci_high: float | None
    method: str
    n_clean_laps: int
    flags: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def unavailable(cls, method: str, n_clean_laps: int, *flags: str) -> DegradationEstimate:
        return cls(None, None, None, method, n_clean_laps, tuple(flags))


class DegradationModel(Protocol):
    name: str

    def fit(self, df: pd.DataFrame) -> None: ...
    def predict(self, df: pd.DataFrame) -> FloatArray: ...
    def predict_interval(self, df: pd.DataFrame, alpha: float) -> tuple[FloatArray, FloatArray]: ...
    def degradation_per_lap(self, ctx: StintContext) -> DegradationEstimate: ...
