"""Per-corner and per-straight metrics for one aligned lap. Pure functions on the grid.

Every function returns None (never 0, never NaN) when the quantity does not exist:
no braking in a flat-out corner, no full throttle before the next corner, a window
outside the lap. Braking comes from the boolean brake channel only; brake pressure
is not available and nothing here should be read as a pressure difference.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import savgol_filter

from ..ingestion.models import BoolArray, FloatArray
from .config import Thresholds
from .models import AlignedLap


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def diff(target: float | None, reference: float | None) -> float | None:
    """target − reference, or None when either side is missing."""
    if target is None or reference is None:
        return None
    return target - reference


def _index(grid: FloatArray, d: float) -> int:
    return int(np.clip(np.searchsorted(grid, d, side="left"), 0, len(grid) - 1))


def _clip(grid: FloatArray, d: float) -> float:
    return float(min(max(d, float(grid[0])), float(grid[-1])))


def value_at(grid: FloatArray, values: FloatArray, d: float) -> float | None:
    if d < grid[0] or d > grid[-1]:
        return None
    return _finite(float(np.interp(d, grid, values)))


def speed_at(grid: FloatArray, lap: AlignedLap, d: float) -> float | None:
    return value_at(grid, lap.speed_kph, d)


def time_at(grid: FloatArray, lap: AlignedLap, d: float) -> float | None:
    return value_at(grid, lap.time_s, d)


def _run_end(mask: BoolArray, start: int) -> int:
    """Index of the first False at or after start (len(mask) if the run reaches the end)."""
    off = np.flatnonzero(~mask[start:])
    return start + int(off[0]) if len(off) else len(mask)


def _sustained_start(
    grid: FloatArray, lap: AlignedLap, mask: BoolArray, lo: float, hi: float, min_duration_s: float,
) -> tuple[int, int] | None:
    """First index in [lo, hi] where `mask` turns on and stays on ≥ min_duration_s.
    A run already on at lo counts from lo. Returns (start, run_end)."""
    i, last = _index(grid, lo), _index(grid, hi)
    if grid[last] > hi:
        last -= 1
    while i <= last:
        if mask[i]:
            end = _run_end(mask, i)
            t_end = lap.time_s[min(end, len(mask) - 1)]
            if t_end - lap.time_s[i] >= min_duration_s:
                return i, end
            i = end
        else:
            on = np.flatnonzero(mask[i:last + 1])
            if not len(on):
                return None
            i += int(on[0])
    return None


def braking_start_m(grid: FloatArray, lap: AlignedLap, lo: float, hi: float, min_duration_s: float) -> float | None:
    found = _sustained_start(grid, lap, lap.brake, lo, hi, min_duration_s)
    return None if found is None else float(grid[found[0]])


def _braking_profile(
    grid: FloatArray, lap: AlignedLap, lo: float, hi: float, thresholds: Thresholds,
) -> tuple[float | None, float | None, float | None]:
    """(braking_start_m, braking_duration_s, peak_decel_ms2) — the last two informational."""
    found = _sustained_start(grid, lap, lap.brake, lo, hi, thresholds.brake_min_duration_s)
    if found is None:
        return None, None, None
    start, end = found
    end = min(end, len(grid) - 1)
    duration = float(lap.time_s[end] - lap.time_s[start])
    window = max(int(round(thresholds.apex_smooth_m / max(float(grid[1] - grid[0]), 1e-9))) | 1, 3)
    speed_ms = np.nan_to_num(lap.speed_kph, nan=0.0) / 3.6
    if len(speed_ms) > window:
        speed_ms = savgol_filter(speed_ms, window, 2, mode="interp")
    decel = -np.gradient(speed_ms, lap.time_s)[start:end + 1]
    peak = _finite(float(np.max(decel))) if len(decel) else None
    return float(grid[start]), duration, peak


def min_speed(grid: FloatArray, lap: AlignedLap, apex_m: float, window_m: float) -> tuple[float | None, float | None]:
    lo, hi = _index(grid, apex_m - window_m), _index(grid, apex_m + window_m)
    speeds = lap.speed_kph[lo:hi + 1]
    if not len(speeds) or not np.isfinite(speeds).any():
        return None, None
    i = lo + int(np.nanargmin(speeds))
    return float(lap.speed_kph[i]), float(grid[i])


def throttle_reapply_m(grid: FloatArray, lap: AlignedLap, apex_m: float, end_m: float, pct: float) -> float | None:
    lo, hi = _index(grid, apex_m), _index(grid, end_m)
    on = np.flatnonzero(np.nan_to_num(lap.throttle_pct[lo:hi + 1], nan=0.0) >= pct)
    return float(grid[lo + int(on[0])]) if len(on) else None


def full_throttle_m(
    grid: FloatArray, lap: AlignedLap, apex_m: float, end_m: float, pct: float, min_duration_s: float,
) -> float | None:
    mask = np.nan_to_num(lap.throttle_pct, nan=0.0) >= pct
    found = _sustained_start(grid, lap, mask, apex_m, end_m, min_duration_s)
    return None if found is None else float(grid[found[0]])


def gear_stats(grid: FloatArray, lap: AlignedLap, start_m: float, end_m: float, apex_m: float) -> tuple[int | None, int | None]:
    lo, hi = _index(grid, start_m), _index(grid, end_m)
    gears = lap.gear[lo:hi + 1]
    gears = gears[gears > 0]  # 0 = unknown/neutral in FastF1 data
    at_apex = int(lap.gear[_index(grid, apex_m)])
    return (int(gears.min()) if len(gears) else None), (at_apex if at_apex > 0 else None)


@dataclass(frozen=True)
class CornerLapMetrics:
    braking_start_m: float | None
    braking_duration_s: float | None
    peak_decel_ms2: float | None
    entry_speed_kph: float | None
    min_speed_kph: float | None
    min_speed_m: float | None
    exit_speed_kph: float | None
    throttle_reapply_m: float | None
    full_throttle_m: float | None
    time_to_full_throttle_s: float | None
    gear_min: int | None
    gear_at_apex: int | None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def corner_lap_metrics(
    grid: FloatArray, lap: AlignedLap, start_m: float, end_m: float, apex_m: float, thresholds: Thresholds,
) -> CornerLapMetrics:
    brake_lo = max(apex_m - thresholds.braking_window_m, start_m)
    braking, duration, decel = _braking_profile(grid, lap, brake_lo, apex_m, thresholds)
    entry_at = braking if braking is not None else _clip(grid, apex_m - thresholds.entry_fallback_m)
    v_min, v_min_m = min_speed(grid, lap, apex_m, thresholds.min_speed_window_m)
    full = full_throttle_m(grid, lap, apex_m, end_m, thresholds.full_throttle_pct, thresholds.full_throttle_min_s)
    t_apex = time_at(grid, lap, apex_m)
    t_full = time_at(grid, lap, full) if full is not None else None
    gear_min, gear_apex = gear_stats(grid, lap, start_m, end_m, apex_m)
    return CornerLapMetrics(
        braking_start_m=braking,
        braking_duration_s=duration,
        peak_decel_ms2=decel,
        entry_speed_kph=speed_at(grid, lap, entry_at),
        min_speed_kph=v_min,
        min_speed_m=v_min_m,
        exit_speed_kph=speed_at(grid, lap, _clip(grid, apex_m + thresholds.exit_offset_m)),
        throttle_reapply_m=throttle_reapply_m(grid, lap, apex_m, end_m, thresholds.throttle_pickup_pct),
        full_throttle_m=full,
        time_to_full_throttle_s=diff(t_full, t_apex),
        gear_min=gear_min,
        gear_at_apex=gear_apex,
    )


@dataclass(frozen=True)
class StraightLapMetrics:
    top_speed_kph: float | None
    top_speed_m: float | None
    entry_speed_kph: float | None
    exit_speed_kph: float | None
    drs_open_fraction: float | None
    time_s: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def straight_lap_metrics(grid: FloatArray, lap: AlignedLap, start_m: float, end_m: float) -> StraightLapMetrics:
    lo, hi = _index(grid, start_m), _index(grid, end_m)
    speeds = lap.speed_kph[lo:hi + 1]
    top: float | None = None
    top_m: float | None = None
    if len(speeds) and np.isfinite(speeds).any():
        i = lo + int(np.nanargmax(speeds))
        top, top_m = float(lap.speed_kph[i]), float(grid[i])
    drs = lap.drs_open[lo:hi + 1]
    return StraightLapMetrics(
        top_speed_kph=top,
        top_speed_m=top_m,
        entry_speed_kph=speed_at(grid, lap, start_m),
        exit_speed_kph=speed_at(grid, lap, end_m),
        drs_open_fraction=float(drs.mean()) if len(drs) else None,
        time_s=diff(time_at(grid, lap, end_m), time_at(grid, lap, start_m)),
    )
