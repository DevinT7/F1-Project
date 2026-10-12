"""Lap cleaning, length normalization and resampling onto a shared distance grid.

Implements the Phase 2 algorithm exactly:

1. clean_lap — sort by time, drop duplicate timestamps, force distance non-decreasing,
   drop non-advancing samples at speed (> 5 km/h = glitch), nudge the remaining
   stationary samples by an epsilon so distance is strictly increasing (counted as
   repairs), re-anchor time and distance at 0, and reject laps with < 200 samples or
   a duration more than 0.15 s from the official lap time.
2. normalize_length — scale the target's distance by L_ref / L_target (both laps cross
   the same line, so length differences are integration drift); > 3 % is rejected.
3. make_grid / resample_to_grid — common grid 0..L_ref ending exactly on L_ref;
   continuous channels interpolate linearly, gear/brake/DRS use previous-sample steps.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import structlog

from ..ingestion.models import FloatArray
from .errors import LapInvalidError
from .models import AlignedLap, CleanLap, LapData

log = structlog.get_logger("raceiq.processing")

MIN_SAMPLES = 200
DURATION_TOLERANCE_S = 0.15
MAX_LENGTH_DEVIATION = 0.03
GLITCH_SPEED_KPH = 5.0
DISTANCE_EPSILON_M = 1e-6


def clean_lap(lap: LapData) -> CleanLap:
    tel = lap.telemetry
    label = f"{lap.driver or '?'} lap {lap.lap_number}"
    if len(tel) == 0:
        raise LapInvalidError(f"{label}: no telemetry samples.")

    # 1. sort by time, drop duplicate timestamps (keep the first)
    order = np.argsort(tel.time_s, kind="stable")
    t = tel.time_s[order]
    keep = np.concatenate(([True], np.diff(t) > 0))
    idx = order[keep]

    # 2. distance non-decreasing; a sample that does not advance while moving is a glitch
    d = np.maximum.accumulate(tel.distance_m[idx])
    speed = tel.speed_kph[idx]
    stalled = np.concatenate(([False], np.diff(d) <= 0))
    glitch = stalled & (np.nan_to_num(speed, nan=0.0) > GLITCH_SPEED_KPH)
    idx, d = idx[~glitch], d[~glitch]
    dropped = int(glitch.sum())
    if dropped:
        # a glitch run can leave the following samples equal to an earlier distance
        d = np.maximum.accumulate(d)

    if len(idx) < MIN_SAMPLES:
        raise LapInvalidError(f"{label}: only {len(idx)} usable samples (< {MIN_SAMPLES}).")

    # 3. stationary samples (crawling, < 5 km/h) are real; make distance strictly increasing
    d, repairs = _strictly_increasing(d)

    # 4. re-anchor: elapsed lap time and distance from the first sample
    t = tel.time_s[idx] - tel.time_s[idx[0]]
    d = d - d[0]

    duration = float(t[-1])
    if abs(duration - lap.lap_time_s) > DURATION_TOLERANCE_S:
        raise LapInvalidError(
            f"{label}: telemetry duration {duration:.3f}s differs from lap time "
            f"{lap.lap_time_s:.3f}s by more than {DURATION_TOLERANCE_S}s."
        )
    if repairs or dropped:
        log.info("alignment.clean", lap=label, repairs=repairs, dropped_glitches=dropped)

    return CleanLap(
        time_s=t,
        distance_m=d,
        speed_kph=tel.speed_kph[idx],
        throttle_pct=tel.throttle_pct[idx],
        rpm=tel.rpm[idx],
        x=tel.x[idx],
        y=tel.y[idx],
        gear=tel.gear[idx],
        brake=tel.brake[idx],
        drs_open=tel.drs_open[idx],
        lap_time_s=lap.lap_time_s,
        driver=lap.driver,
        lap_number=lap.lap_number,
        sector_s=lap.sector_s,
        sample_count=len(idx),
        repairs=repairs,
        dropped_glitches=dropped,
    )


def _strictly_increasing(d: FloatArray) -> tuple[FloatArray, int]:
    steps = np.diff(d)
    flat = steps <= 0
    repairs = int(flat.sum())
    if not repairs:
        return d, 0
    steps = np.where(flat, DISTANCE_EPSILON_M, steps)
    return np.concatenate(([d[0]], d[0] + np.cumsum(steps))), repairs


def normalize_length(lap: CleanLap, reference_length_m: float) -> CleanLap:
    if lap.length_m <= 0:
        raise LapInvalidError(f"{lap.driver} lap {lap.lap_number}: zero telemetry length.")
    factor = reference_length_m / lap.length_m
    if abs(factor - 1.0) > MAX_LENGTH_DEVIATION:
        raise LapInvalidError(
            f"{lap.driver} lap {lap.lap_number}: length {lap.length_m:.1f} m vs reference "
            f"{reference_length_m:.1f} m (scale {factor:.4f}) exceeds ±{MAX_LENGTH_DEVIATION:.0%}."
        )
    scaled: FloatArray = (lap.distance_m * factor).astype(np.float64)
    scaled[-1] = reference_length_m  # exact, so the grid's last point hits the lap end
    return replace(lap, distance_m=scaled, length_scale_factor=factor)


def make_grid(length_m: float, step_m: float = 1.0) -> FloatArray:
    if step_m <= 0 or length_m <= 0:
        raise ValueError("grid length and step must be positive")
    grid: FloatArray = np.arange(0.0, length_m, step_m, dtype=np.float64)
    if length_m - grid[-1] < step_m * 1e-3:  # avoid a sliver interval before the end
        grid = grid[:-1]
    return np.append(grid, length_m)


def resample_to_grid(lap: CleanLap, grid_m: FloatArray) -> AlignedLap:
    d = lap.distance_m
    if grid_m[-1] > d[-1] + 1e-6:
        raise ValueError(f"grid ends at {grid_m[-1]:.3f} m beyond the lap's {d[-1]:.3f} m")
    step_idx = np.clip(np.searchsorted(d, grid_m, side="right") - 1, 0, len(d) - 1)

    def linear(values: FloatArray) -> FloatArray:
        return np.interp(grid_m, d, values)

    return AlignedLap(
        driver=lap.driver,
        lap_number=lap.lap_number,
        lap_time_s=lap.lap_time_s,
        sector_s=lap.sector_s,
        time_s=linear(lap.time_s),
        speed_kph=linear(lap.speed_kph),
        throttle_pct=linear(lap.throttle_pct),
        rpm=linear(lap.rpm),
        x=linear(lap.x),
        y=linear(lap.y),
        gear=lap.gear[step_idx],
        brake=lap.brake[step_idx],
        drs_open=lap.drs_open[step_idx],
        sample_count=lap.sample_count,
        length_scale_factor=lap.length_scale_factor,
    )
