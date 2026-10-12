"""Normalized domain models for ingested sessions.

Every module outside `fastf1_client.py` consumes these, never FastF1 objects. All times
are float seconds, all datetimes are timezone-aware UTC, and missing values are None.
Bump SCHEMA_VERSION whenever a stored shape changes; stored sessions with an older
version are treated as absent and get re-ingested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, field_validator

SCHEMA_VERSION = 1

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]
BoolArray = npt.NDArray[np.bool_]


def _require_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionInfo(_Model):
    key: str                          # "2024_05_Q"
    year: int
    round: int
    event_name: str
    circuit: str                      # FastF1 event Location, e.g. "Shanghai"
    country: str
    session_code: str                 # FP1 | FP2 | FP3 | SQ | SS | S | Q | R
    session_name: str                 # "Qualifying", "Race", ...
    date: datetime                    # session start, UTC
    n_laps_scheduled: int | None      # races/sprints only
    event_format: str | None = None   # "conventional", "sprint_qualifying", ...
    circuit_rotation_deg: float | None = None

    @field_validator("date")
    @classmethod
    def _date_utc(cls, value: datetime) -> datetime:
        out = _require_utc(value)
        assert out is not None
        return out


class DriverInfo(_Model):
    code: str                         # "VER"
    number: str                       # "1" (string: FastF1 keys drivers by it)
    full_name: str | None
    team: str | None
    team_color: str | None            # "#3671C6"
    position: int | None = None       # classified position in this session
    grid_position: int | None = None
    status: str | None = None
    q1_s: float | None = None
    q2_s: float | None = None
    q3_s: float | None = None


class LapRecord(_Model):
    session_key: str
    driver: str
    driver_number: str
    team: str | None
    lap_number: int
    lap_time_s: float | None
    sector_s: tuple[float | None, float | None, float | None]
    stint: int | None
    compound: str | None
    tyre_age: int | None              # laps on this set, FastF1 TyreLife
    fresh_tyre: bool | None
    pit_in_time_s: float | None       # session time
    pit_out_time_s: float | None      # session time
    track_status: str | None          # FastF1 concatenated status codes, e.g. "12"
    is_accurate: bool
    deleted: bool | None              # None = not determinable (no race control data)
    deleted_reason: str | None
    position: int | None
    lap_start_session_time_s: float | None
    lap_end_session_time_s: float | None
    air_temp_c: float | None = None
    track_temp_c: float | None = None
    rainfall: bool | None = None
    telemetry_samples: int = 0
    telemetry_distance_m: float | None = None
    is_valid_for_comparison: bool = False
    invalid_reason: str | None = None


class CornerInfo(_Model):
    number: int
    letter: str | None
    distance_m: float
    x: float
    y: float
    angle: float


class WeatherSample(_Model):
    time_s: float                     # session time
    air_temp_c: float | None
    track_temp_c: float | None
    humidity: float | None
    rainfall: bool | None
    wind_kph: float | None


class EventSession(_Model):
    name: str                         # "Qualifying"
    code: str | None                  # "Q"; None for session names we do not map
    date: datetime | None             # UTC

    @field_validator("date")
    @classmethod
    def _date_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)


class EventInfo(_Model):
    year: int
    round: int
    event_name: str
    country: str
    location: str
    event_format: str
    event_date: datetime | None
    sessions: tuple[EventSession, ...]

    @field_validator("event_date")
    @classmethod
    def _date_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value)


class Manifest(_Model):
    session_key: str
    ingested_at: datetime
    fastf1_version: str
    schema_version: int
    lap_count: int
    telemetry_count: int
    driver_count: int

    @field_validator("ingested_at")
    @classmethod
    def _date_utc(cls, value: datetime) -> datetime:
        out = _require_utc(value)
        assert out is not None
        return out


TELEMETRY_COLUMNS: tuple[str, ...] = (
    "time_s", "distance_m", "speed_kph", "throttle_pct", "brake", "gear",
    "rpm", "drs_code", "drs_open", "x", "y",
)
_FLOAT_COLUMNS = frozenset({"time_s", "distance_m", "speed_kph", "throttle_pct", "rpm", "x", "y"})
_INT_COLUMNS = frozenset({"gear", "drs_code"})


@dataclass(frozen=True, eq=False)
class TelemetryFrame:
    """One lap of telemetry, column-oriented. time_s is lap-relative and strictly
    increasing; distance_m is non-decreasing. Float channels may contain NaN."""

    time_s: FloatArray
    distance_m: FloatArray
    speed_kph: FloatArray
    throttle_pct: FloatArray
    brake: BoolArray
    gear: IntArray
    rpm: FloatArray
    drs_code: IntArray
    drs_open: BoolArray
    x: FloatArray
    y: FloatArray

    def __post_init__(self) -> None:
        lengths = {name: len(getattr(self, name)) for name in TELEMETRY_COLUMNS}
        if len(set(lengths.values())) > 1:
            raise ValueError(f"telemetry columns have unequal lengths: {lengths}")

    def __len__(self) -> int:
        return len(self.time_s)

    @property
    def total_distance_m(self) -> float | None:
        if len(self) == 0:
            return None
        return float(self.distance_m[-1] - self.distance_m[0])

    def columns(self) -> dict[str, npt.NDArray[Any]]:
        return {name: getattr(self, name) for name in TELEMETRY_COLUMNS}

    @classmethod
    def from_columns(cls, columns: dict[str, Any]) -> TelemetryFrame:
        missing = [name for name in TELEMETRY_COLUMNS if name not in columns]
        if missing:
            raise ValueError(f"telemetry is missing columns: {missing}")
        typed: dict[str, Any] = {}
        for name in TELEMETRY_COLUMNS:
            if name in _FLOAT_COLUMNS:
                typed[name] = np.asarray(columns[name], dtype=np.float64)
            elif name in _INT_COLUMNS:
                typed[name] = np.asarray(columns[name], dtype=np.int64)
            else:
                typed[name] = np.asarray(columns[name], dtype=np.bool_)
        return cls(**typed)

    @classmethod
    def empty(cls) -> TelemetryFrame:
        return cls.from_columns({name: [] for name in TELEMETRY_COLUMNS})


LapKey = tuple[str, int]  # (driver code, lap number)


@dataclass(frozen=True)
class SessionBundle:
    """Everything ingested for one session, ready to hand to a repository."""

    info: SessionInfo
    drivers: list[DriverInfo]
    laps: list[LapRecord]
    weather: list[WeatherSample]
    corners: list[CornerInfo]
    telemetry: dict[LapKey, TelemetryFrame] = field(default_factory=dict)
    corners_raw: list[dict[str, Any]] = field(default_factory=list)
    fastf1_version: str = "unknown"
