"""POST /api/v1/comparisons request and response.

All arrays in the response have the same length and are aligned to `distance_m`.
Booleans (brake, drs_open) are JSON booleans, never 0/1. Missing samples are null.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...processing.insights import FindingsReport, ReportQuality
from .common import SESSION_KEY_DESCRIPTION, canonical_session_key

SIGN_CONVENTION = (
    "delta_s = target elapsed time − reference elapsed time at the same distance. "
    "Positive = the target is behind (has lost time); negative = the target is ahead. "
    "delta_s[0] is 0 and delta_s[-1] equals the official lap-time gap (target − reference) within 0.02 s."
)
MIN_RESOLUTION_M, MAX_RESOLUTION_M, DEFAULT_RESOLUTION_M = 1.0, 10.0, 2.0


class LapSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver: str = Field(description="driver code (VER) or number (1)", examples=["VER"])
    lap: int | Literal["fastest"] = Field(
        default="fastest", description='lap number, or "fastest" for the driver\'s fastest valid lap'
    )


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{
            "session_key": "2024-05-Q",
            "reference": {"driver": "VER", "lap": "fastest"},
            "target": {"driver": "ALO", "lap": 15},
            "resolution_m": 2.0,
        }]},
    )
    session_key: str = Field(description=SESSION_KEY_DESCRIPTION)
    reference: LapSelector
    target: LapSelector
    resolution_m: float = Field(
        default=DEFAULT_RESOLUTION_M, ge=MIN_RESOLUTION_M, le=MAX_RESOLUTION_M,
        description="transport resolution in metres (≈2,500–3,500 points at 2 m). "
                    "Analysis always runs at 1 m; this only thins the returned arrays.",
    )

    @field_validator("session_key")
    @classmethod
    def _key(cls, value: str) -> str:
        canonical_session_key(value)
        return value


class LapChannels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_s: list[float] = Field(description="elapsed lap time at each distance")
    speed_kph: list[float | None]
    throttle_pct: list[float | None]
    brake: list[bool] = Field(description="on/off brake channel (no pressure)")
    gear: list[int] = Field(description="step-interpolated, never fractional; 0 = unknown")
    rpm: list[float | None]
    drs_open: list[bool]
    x: list[float | None] = Field(description="FastF1 position, decimetres")
    y: list[float | None] = Field(description="FastF1 position, decimetres")


class ComparisonChannels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: LapChannels
    target: LapChannels


class LapMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver: str
    driver_number: str | None = None
    full_name: str | None = None
    team: str | None = None
    team_color: str | None = None
    lap_number: int
    lap_time_s: float
    sector_s: list[float | None]
    compound: str | None = None
    tyre_age_laps: int | None = None


class ComparisonMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_key: str
    resolution_m: float
    points: int = Field(description="length of every array in the response")
    sign_convention: str = Field(default=SIGN_CONVENTION)
    reference: LapMeta
    target: LapMeta
    quality: ReportQuality
    cached: bool = Field(description="served from the in-process comparison cache")


class ComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ComparisonMeta
    distance_m: list[float] = Field(description="reference-lap distance grid, metres from the start line")
    channels: ComparisonChannels
    delta_s: list[float] = Field(description=SIGN_CONVENTION)
    findings: FindingsReport = Field(
        description="Phase 3 verified findings, computed at 1 m. time_loss_s > 0 = target lost time "
                    "(same sign convention as delta_s). See docs/insight_schema.md."
    )
