"""Schemas for /api/v1/tires. Floats are finite by construction (allow_inf_nan=False)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PredictedLap(_Model):
    lap: int
    pred_s: float = Field(description="predicted lap time (s)")
    lo_s: float = Field(description="lower bound of the prediction interval (s)")
    hi_s: float = Field(description="upper bound of the prediction interval (s)")


class PitWindow(_Model):
    earliest_lap: int | None = Field(description="first lap the pessimistic curve reaches the threshold")
    central_lap: int | None = Field(description="first lap the forecast reaches the threshold")
    latest_lap: int | None = Field(description="first lap the optimistic curve reaches the threshold; null = "
                                               "no crossing within the horizon")
    threshold_s: float = Field(description="X: seconds above best pace that count as 'tyre gone' (a heuristic, "
                                           "not a team rule)")
    threshold_pace_s: float = Field(description="best pace + X, on the pace basis below")
    best_pace_s: float = Field(description="median of the 3 fastest clean laps of the stint so far")
    pace_basis: Literal["fuel_adjusted_model_term", "raw"]
    method: Literal["interval_crossing_v2"]
    confidence: Literal["high", "medium", "low"]
    assumptions: list[str]
    notes: list[str]


class ModelRef(_Model):
    name: str
    version: str
    trained_through: str


class TirePrediction(_Model):
    status: Literal["ok", "insufficient_data"]
    reason: str | None = Field(None, description="why status is insufficient_data")
    session_key: str
    driver: str
    lap: int = Field(description="the cut: only laps ≤ this one were used")
    horizon: int
    stint: int | None = None
    compound: str | None = None
    tyre_age: int | None = None
    clean_laps_used: int | None = None
    degradation_s_per_lap: float | None = None
    degradation_ci: list[float] | None = Field(None, description="95% interval [low, high]", min_length=2, max_length=2)
    predicted_laps: list[PredictedLap] | None = Field(None, description="consecutive laps from lap + 1")
    pit_window: PitWindow | None = None
    pit_window_reason: str | None = Field(None, description="why pit_window is null, e.g. forecast_uncertainty_too_high")
    remaining_competitive_laps: int | None = Field(None, description="central_lap − lap, when defined")
    model: ModelRef | None = None
    warnings: list[str] = Field(default_factory=list, description="every null field is explained here")


class TireModelInfo(_Model):
    name: str
    version: str
    kind: str
    status: Literal["production", "candidate"]
    dataset_hash: str
    git_sha: str
    trained_through: str
    trained_through_session: str
    interval_alpha: float
    metrics: dict[str, Any] = Field(description="6D evaluation of this configuration (holdout once; walk-forward)")
    notes: str | None = None


class TireModelsResponse(_Model):
    models: list[TireModelInfo]


PREDICTION_EXAMPLE: dict[str, Any] = {
    "status": "ok", "reason": None, "session_key": "2024-14-R", "driver": "LEC", "lap": 8, "horizon": 3,
    "stint": 1, "compound": "MEDIUM", "tyre_age": 8, "clean_laps_used": 7,
    "degradation_s_per_lap": 0.0472, "degradation_ci": [0.0398, 0.0547],
    "predicted_laps": [{"lap": 9, "pred_s": 109.843, "lo_s": 108.995, "hi_s": 110.69},
                       {"lap": 10, "pred_s": 109.836, "lo_s": 108.901, "hi_s": 110.771},
                       {"lap": 11, "pred_s": 109.83, "lo_s": 108.877, "hi_s": 110.783}],
    "pit_window": None, "pit_window_reason": "forecast_uncertainty_too_high", "remaining_competitive_laps": None,
    "model": {"name": "baseline_b_conformal", "version": "2024-07-28.6681d16a", "trained_through": "2024-07-28"},
    "warnings": ["pit_window: forecast_uncertainty_too_high (interval width 4.19 s at lap 33 exceeds 2 × 1 s)"],
}
INSUFFICIENT_EXAMPLE: dict[str, Any] = {
    "status": "insufficient_data", "reason": "1 clean laps in this stint so far; at least 5 needed",
    "session_key": "2024-14-R", "driver": "LEC", "lap": 14, "horizon": 5, "stint": 2, "compound": "HARD",
    "tyre_age": 2, "clean_laps_used": 1, "degradation_s_per_lap": None, "degradation_ci": None, "predicted_laps": None,
    "pit_window": None, "pit_window_reason": None, "remaining_competitive_laps": None, "model": {
        "name": "baseline_b_conformal", "version": "2024-07-28.6681d16a", "trained_through": "2024-07-28"},
    "warnings": [],
}
