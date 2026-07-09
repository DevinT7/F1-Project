"""
SHARED CONTRACT — the single source of truth for every API response shape.

All four workstreams code against these models. `frontend/src/types.ts` mirrors them.
Change these via PR with a second reviewer so backend + frontend never drift.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- Sessions / meta
class Session(BaseModel):
    session_key: int
    meeting_key: int
    year: int
    country_name: str | None = None
    circuit_short_name: str | None = None
    session_name: str | None = None          # "Race", "Qualifying", ...
    session_type: str | None = None
    date_start: str | None = None


class Driver(BaseModel):
    driver_number: int
    full_name: str | None = None
    name_acronym: str | None = None          # "VER"
    team_name: str | None = None
    team_colour: str | None = None           # hex without '#'
    headshot_url: str | None = None


# ---------------------------------------------------------------- Telemetry
class TelemetrySample(BaseModel):
    distance: float                          # metres from start/finish (derived)
    speed: float | None = None               # km/h
    throttle: float | None = None            # 0-100
    brake: float | None = None               # 0-100
    n_gear: int | None = None
    rpm: float | None = None
    drs: int | None = None


class TelemetryTrace(BaseModel):
    session_key: int
    driver_number: int
    lap_number: int | None = None
    lap_time: float | None = None            # seconds
    samples: list[TelemetrySample]


# ---------------------------------------------------------------- Analysis
class CornerInsight(BaseModel):
    corner_number: int
    corner_name: str | None = None
    distance: float
    entry_speed: float | None = None
    min_speed: float | None = None
    exit_speed: float | None = None
    braking_point_m: float | None = None
    throttle_delay_s: float | None = None
    time_delta_s: float                      # negative = selected driver faster


class SectorDelta(BaseModel):
    sector: int
    delta_s: float


class DeltaPoint(BaseModel):
    distance: float
    delta_s: float                           # cumulative time delta vs reference


class LapComparison(BaseModel):
    session_key: int
    driver: Driver
    reference: Driver
    lap_time: float | None = None
    reference_lap_time: float | None = None
    total_delta_s: float
    time_gained_s: float
    time_lost_s: float
    sector_deltas: list[SectorDelta]
    delta_trace: list[DeltaPoint]
    corners: list[CornerInsight]
    driver_trace: TelemetryTrace | None = None
    reference_trace: TelemetryTrace | None = None


# ---------------------------------------------------------------- Tires
class LapPacePrediction(BaseModel):
    laps_ahead: int
    predicted_lap_time_s: float
    lower_s: float
    upper_s: float


class TireAnalysis(BaseModel):
    session_key: int
    driver_number: int
    compound: str | None = None
    tyre_age_laps: int | None = None
    degradation_s_per_lap: float
    remaining_competitive_laps: int | None = None
    recommended_pit_window: tuple[int, int] | None = None
    model_confidence: float = Field(ge=0, le=1)
    predictions: list[LapPacePrediction]
    model_name: str = "baseline-linear"


# ---------------------------------------------------------------- AI engineer
class AIReportRequest(BaseModel):
    comparison: LapComparison | None = None
    tires: TireAnalysis | None = None
    question: str | None = None              # optional follow-up


class AIInsight(BaseModel):
    label: str
    delta_s: float | None = None


class AIReport(BaseModel):
    summary: str
    insights: list[AIInsight]
    recommendations: list[str]
    strategy_options: list[str]
    source: str                              # "llm" or "template"
    grounded: bool = True                    # numbers came from computed findings only
