"""Models for the lap alignment / delta engine.

LapData is the engine's input (one lap's telemetry + official timing). AlignedLap is a
lap resampled onto a shared distance grid, and LapComparison is the full output of
compare_laps. Arrays are numpy; times are seconds, distances metres.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..ingestion.models import BoolArray, FloatArray, IntArray, TelemetryFrame

if TYPE_CHECKING:
    from ..storage.repository import SessionRepository

SectorTimes = tuple[float | None, float | None, float | None]


@dataclass(frozen=True, eq=False)
class LapData:
    telemetry: TelemetryFrame
    lap_time_s: float
    driver: str = ""
    lap_number: int = 0
    sector_s: SectorTimes = (None, None, None)

    @classmethod
    def from_repository(cls, repo: SessionRepository, key: str, driver: str, lap_number: int) -> LapData:
        from .errors import LapInvalidError

        match = [lap for lap in repo.list_laps(key, driver) if lap.lap_number == lap_number]
        lap_time = match[0].lap_time_s if match else None
        if lap_time is None:
            raise LapInvalidError(f"{driver} lap {lap_number} has no lap time in {key}.")
        lap = match[0]
        return cls(
            telemetry=repo.get_telemetry(key, lap.driver, lap_number),
            lap_time_s=lap_time,
            driver=lap.driver,
            lap_number=lap_number,
            sector_s=lap.sector_s,
        )


@dataclass(frozen=True, eq=False)
class CleanLap:
    """A lap after clean_lap: time/distance re-anchored at 0, distance strictly increasing."""

    time_s: FloatArray
    distance_m: FloatArray
    speed_kph: FloatArray
    throttle_pct: FloatArray
    rpm: FloatArray
    x: FloatArray
    y: FloatArray
    gear: IntArray
    brake: BoolArray
    drs_open: BoolArray
    lap_time_s: float
    driver: str
    lap_number: int
    sector_s: SectorTimes
    sample_count: int
    repairs: int = 0                 # equal-distance samples nudged by epsilon
    dropped_glitches: int = 0        # non-advancing samples at speed, removed
    length_scale_factor: float = 1.0
    alignment: str = "distance"           # "xy": distance corrected by X/Y projection (xy_align)
    xy_correction_rms_m: float | None = None
    xy_correction_max_m: float | None = None

    @property
    def length_m(self) -> float:
        return float(self.distance_m[-1])

    @property
    def duration_s(self) -> float:
        return float(self.time_s[-1])


@dataclass(frozen=True, eq=False)
class AlignedLap:
    """One lap on the shared grid. time_s is elapsed lap time at each grid distance."""

    driver: str
    lap_number: int
    lap_time_s: float
    sector_s: SectorTimes
    time_s: FloatArray
    speed_kph: FloatArray
    throttle_pct: FloatArray
    rpm: FloatArray
    x: FloatArray
    y: FloatArray
    gear: IntArray
    brake: BoolArray
    drs_open: BoolArray
    sample_count: int
    length_scale_factor: float


@dataclass(frozen=True)
class ComparisonQuality:
    sample_count_ref: int
    sample_count_target: int
    length_scale_factor: float       # applied to the target's distance
    final_delta_error_s: float       # delta_s[-1] − (target − reference lap time)
    repairs: int                     # epsilon repairs across both laps
    dropped_glitches: int
    alignment: str = "distance"      # "xy" = target distance corrected by X/Y projection
    xy_correction_rms_m: float | None = None
    xy_correction_max_m: float | None = None


@dataclass(frozen=True, eq=False)
class LapComparison:
    grid_m: FloatArray
    reference: AlignedLap
    target: AlignedLap
    delta_s: FloatArray              # target − reference; > 0 means target is behind
    delta_rate: FloatArray           # d(delta)/d(distance), s/m, unsmoothed
    delta_rate_smoothed: FloatArray  # Savitzky–Golay, for display only
    speed_diff_kph: FloatArray       # target − reference
    quality: ComparisonQuality

    @property
    def step_m(self) -> float:
        return float(self.grid_m[1] - self.grid_m[0]) if len(self.grid_m) > 1 else 0.0


@dataclass(frozen=True)
class SectorCheck:
    sector: int                      # boundary after sector N (1 or 2), or 3 = finish line
    boundary_m: float                # estimated from the reference lap's t(d)
    official_delta_s: float          # cumulative official sector-time gap at the boundary
    grid_delta_s: float              # delta_s interpolated at boundary_m
    discrepancy_s: float
    ok: bool


@dataclass(frozen=True)
class SectorDiagnostic:
    available: bool                  # False when either lap lacks sector times
    sector_deltas_s: tuple[float | None, float | None, float | None]
    checks: tuple[SectorCheck, ...]
    tolerance_s: float

    @property
    def ok(self) -> bool:
        return self.available and all(check.ok for check in self.checks)
