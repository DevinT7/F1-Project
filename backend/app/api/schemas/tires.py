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


# Real outputs: Spa 2024, LEC, production model (scripts regenerate them with the registry).
PREDICTION_EXAMPLE: dict[str, Any] = {'status': 'ok', 'reason': None, 'session_key': '2024-14-R', 'driver': 'LEC', 'lap': 39, 'horizon': 3, 'stint': 3, 'compound': 'HARD', 'tyre_age': 14, 'clean_laps_used': 11, 'degradation_s_per_lap': 0.0338, 'degradation_ci': [0.0289, 0.0387], 'predicted_laps': [{'lap': 40, 'pred_s': 107.392, 'lo_s': 106.838, 'hi_s': 107.947}, {'lap': 41, 'pred_s': 107.373, 'lo_s': 106.735, 'hi_s': 108.01}, {'lap': 42, 'pred_s': 107.353, 'lo_s': 106.64, 'hi_s': 108.065}], 'pit_window': {'earliest_lap': 40, 'central_lap': 41, 'latest_lap': None, 'threshold_s': 1.0, 'threshold_pace_s': 107.183, 'best_pace_s': 106.183, 'pace_basis': 'fuel_adjusted_model_term', 'method': 'interval_crossing_v2', 'confidence': 'low', 'assumptions': ['ignores undercut and overcut', 'ignores traffic', 'ignores track position', 'ignores safety-car probability', 'ignores tyre availability (sets left)', 'threshold is a heuristic (best pace + X seconds), not a team rule'], 'notes': ['no crossing within horizon']}, 'pit_window_reason': None, 'remaining_competitive_laps': 2, 'model': {'name': 'baseline_b_recent_conformal', 'version': '2024-07-28.6681d16a', 'trained_through': '2024-07-28'}, 'warnings': []}
UNCERTAIN_EXAMPLE: dict[str, Any] = {'status': 'ok', 'reason': None, 'session_key': '2024-14-R', 'driver': 'LEC', 'lap': 34, 'horizon': 3, 'stint': 3, 'compound': 'HARD', 'tyre_age': 9, 'clean_laps_used': 6, 'degradation_s_per_lap': 0.0338, 'degradation_ci': [0.0289, 0.0387], 'predicted_laps': [{'lap': 35, 'pred_s': 106.981, 'lo_s': 106.426, 'hi_s': 107.535}, {'lap': 36, 'pred_s': 106.961, 'lo_s': 106.324, 'hi_s': 107.598}, {'lap': 37, 'pred_s': 106.941, 'lo_s': 106.229, 'hi_s': 107.654}], 'pit_window': None, 'pit_window_reason': 'forecast_uncertainty_too_high', 'remaining_competitive_laps': None, 'model': {'name': 'baseline_b_recent_conformal', 'version': '2024-07-28.6681d16a', 'trained_through': '2024-07-28'}, 'warnings': ['pit_window: forecast_uncertainty_too_high (interval width 2.06 s at lap 44 exceeds 2 × 1 s)']}
INSUFFICIENT_EXAMPLE: dict[str, Any] = {'status': 'insufficient_data', 'reason': '1 clean laps in this stint so far; at least 5 needed', 'session_key': '2024-14-R', 'driver': 'LEC', 'lap': 14, 'horizon': 5, 'stint': 2, 'compound': 'HARD', 'tyre_age': 2, 'clean_laps_used': 1, 'degradation_s_per_lap': None, 'degradation_ci': None, 'predicted_laps': None, 'pit_window': None, 'pit_window_reason': None, 'remaining_competitive_laps': None, 'model': {'name': 'baseline_b_recent_conformal', 'version': '2024-07-28.6681d16a', 'trained_through': '2024-07-28'}, 'warnings': []}
