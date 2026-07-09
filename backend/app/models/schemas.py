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
    # True when lap_time is a statistical outlier vs. this driver's other laps
    # this session -- most likely traffic, a safety car / VSC period, or a red
    # flag, none of which make for a fair point-for-point comparison. Still
    # returned (never silently swapped for a different lap) so an explicit
    # lap_number request is honored; callers should surface this rather than
    # treat the comparison as routine. See LapComparison.warnings.
    is_anomalous_lap: bool = False
    samples: list[TelemetrySample]


# ---------------------------------------------------------------- Analysis
#
# Consumer guide (Phase 4) — the exact fields P3 and P4 should read, so nobody
# has to reverse-engineer sign conventions or re-derive numbers P2 already
# computed:
#
#   LapComparison.total_delta_s / time_gained_s / time_lost_s (seconds)
#       Sign convention throughout: NEGATIVE = `driver` is ahead of/faster than
#       `reference`. time_gained_s <= 0, time_lost_s >= 0, and they reconcile
#       exactly with total_delta_s (their sum equals it, to float precision
#       before the 3dp rounding each field carries).
#   LapComparison.sector_deltas[i].delta_s (seconds)
#       Same sign convention, per sector. Real OpenF1 sector times when both
#       laps' `laps` rows have them, otherwise an even-thirds approximation
#       derived from delta_trace — either way it's safe to sum and chart as-is.
#   LapComparison.delta_trace (list of {distance, delta_s})
#       The full-resolution cumulative delta vs distance — this is what P4
#       charts as the "time delta" line. Same sign convention.
#   LapComparison.corners[i] (CornerInsight, one per detected/known corner)
#       time_delta_s: seconds lost/gained in just this corner's window, same
#           sign convention as above (positive = `driver` lost time here).
#       braking_delta_m: metres, `driver`'s braking point minus `reference`'s.
#           Negative = `driver` braked earlier (at a smaller distance value).
#       min_speed_delta_kmh: km/h, `driver`'s min corner speed minus
#           `reference`'s. Negative = `driver` carried less min speed.
#       throttle_delay_s: seconds, `driver`'s distance-to-full-throttle minus
#           `reference`'s, converted to time via the corner's own apex speed.
#           Positive = `driver` got to full throttle later.
#       entry_speed / min_speed / exit_speed / braking_point_m / distance are
#           `driver`'s own absolute values (km/h / km/h / km/h / m / m) — what
#           P4 plots on a track map or speed trace; not diffs.
#   LapComparison.driver_trace / reference_trace: full per-sample telemetry,
#       only populated when the caller passed include_traces=True (P4's heavy
#       chart payload). None when False (P3's light structured-findings-only
#       payload) — never partially populated.
class CornerInsight(BaseModel):
    corner_number: int
    corner_name: str | None = None
    distance: float
    entry_speed: float | None = None
    min_speed: float | None = None
    exit_speed: float | None = None
    braking_point_m: float | None = None
    braking_delta_m: float | None = None     # driver - reference; negative = driver braked earlier
    throttle_delay_s: float | None = None    # driver - reference; positive = driver later to full throttle
    min_speed_delta_kmh: float | None = None  # driver - reference; negative = driver carried less speed
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
    # Human-readable caveats about this specific comparison -- e.g. one side's
    # lap looks traffic/safety-car affected. Empty list = no caveats detected.
    # Never blocks the response; always populated from real trace flags, never
    # invented (same grounding guarantee the AI engineer applies to its own output).
    warnings: list[str] = []


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
