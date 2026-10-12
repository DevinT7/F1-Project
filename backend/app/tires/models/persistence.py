"""Persistence benchmark: every future lap = mean of the last 3 clean laps.

No training, no degradation, no fuel. Strong at short horizons, and it must not be hidden: a
model that cannot beat it has learned nothing useful about tyres.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .forecasting import Forecast


class PersistenceModel:
    name = "persistence"

    def __init__(self, window: int = 3) -> None:
        self.window = window

    def params(self) -> dict[str, object]:
        return {"window": self.window}

    def fit(self, train: pd.DataFrame) -> None:  # nothing to learn
        return None

    def forecast(self, history: pd.DataFrame, future: pd.DataFrame) -> Forecast:
        last = history.sort_values("lap_number")["lap_time_s"].to_numpy(dtype=float)[-self.window:]
        return Forecast(mean=np.full(len(future), float(np.mean(last))))
