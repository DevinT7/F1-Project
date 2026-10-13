"""Lap cleaning, length normalization and resampling onto a shared distance grid.

Implements the Phase 2 algorithm exactly:

1. clean_lap — sort by time, drop duplicate timestamps, force distance non-decreasing,
   drop non-advancing samples at speed (> 5 km/h = glitch), nudge the remaining
   stationary samples by an epsilon so distance is strictly increasing (counted as
   repairs), re-anchor time and distance at 0, and reject laps with < 200 samples or
   a duration more than 0.15 s from the official lap time.
2. normalize_length — scale the target's distance by L_ref / L_target (both laps cross
   the same line, so length differences are integration drift); > 3 % is rejected.
3. xy_align (Phase 2 follow-up, 2026-10-13) — re-place the target's distance using its X/Y
   position: each sample is projected onto the reference lap's X/Y path (searching only within
   ±XY_WINDOW_M of its length-normalized distance, so it cannot snap to a neighbouring part of
   the track), giving the reference distance of the same physical point. The offset
   (projected − normalized distance) is integration drift plus position noise; it is low-pass
   filtered over XY_SMOOTH_M and added to the target's distance. The start and finish stay
   pinned at the timing line, so the final delta is unchanged. Falls back to the normalized
   distance when X/Y is missing or the projection is implausible (alignment="distance").
   A constant X/Y time lag cancels: both laps are placed "lag after the same position".
4. make_grid / resample_to_grid — common grid 0..L_ref ending exactly on L_ref;
   continuous channels interpolate linearly, gear/brake/DRS use previous-sample steps.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import numpy.typing as npt
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
AnyArray = npt.NDArray[Any]

XY_UNITS_PER_M = 10.0      # FastF1 X/Y are decimetres
XY_WINDOW_M = 60.0         # search the reference path within ± this of the normalized distance
XY_SMOOTH_M = 150.0        # low-pass window on the projection offset (tuned on 2024_05_Q, docs/progress.md)
XY_MAX_PERP_M = 25.0       # a sample further than this from the reference path is not trusted
XY_MAX_OFFSET_M = 40.0     # |offset| beyond this means the projection failed for that sample
XY_MIN_VALID_FRACTION = 0.9
XY_WEIGHT = 0.5            # blend of two independent ~equal-error estimates (tuned on 2024_05_Q)


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


def _project(ref_x: AnyArray, ref_y: AnyArray, ref_d: AnyArray, qx: AnyArray, qy: AnyArray,
             qd: AnyArray) -> tuple[FloatArray, FloatArray]:
    """For each query point: (reference distance of its projection, perpendicular distance in m),
    searching only reference segments that overlap ±XY_WINDOW_M around qd."""
    n_seg = len(ref_d) - 1
    lo = np.clip(np.searchsorted(ref_d, qd - XY_WINDOW_M, side="left") - 1, 0, n_seg - 1)
    hi = np.clip(np.searchsorted(ref_d, qd + XY_WINDOW_M, side="right"), 1, n_seg)
    width = max(int(np.max(hi - lo)), 1)
    cand = lo[:, None] + np.arange(width)[None, :]
    valid = cand < hi[:, None]
    k = np.minimum(cand, n_seg - 1)
    x0, y0, x1, y1 = ref_x[k], ref_y[k], ref_x[k + 1], ref_y[k + 1]
    vx, vy = x1 - x0, y1 - y0
    seg2 = vx * vx + vy * vy
    safe = np.where(seg2 > 0, seg2, 1.0)
    t = np.clip(np.where(seg2 > 0, ((qx[:, None] - x0) * vx + (qy[:, None] - y0) * vy) / safe, 0.0), 0.0, 1.0)
    dist2 = np.where(valid, (x0 + t * vx - qx[:, None]) ** 2 + (y0 + t * vy - qy[:, None]) ** 2, np.inf)
    best = np.argmin(dist2, axis=1)
    rows = np.arange(len(qd))
    kb, tb = k[rows, best], t[rows, best]
    arc: FloatArray = (ref_d[kb] + tb * (ref_d[kb + 1] - ref_d[kb])).astype(np.float64)
    perp: FloatArray = np.sqrt(dist2[rows, best]).astype(np.float64)
    return arc, perp


def _lowpass(d: AnyArray, values: AnyArray, window_m: float) -> FloatArray:
    """Moving average over window_m of distance (values sampled at increasing d)."""
    grid = np.arange(0.0, float(d[-1]) + 1.0, 1.0)
    on_grid = np.interp(grid, d, values)
    w = max(int(round(window_m)), 1) | 1
    smooth = np.convolve(np.pad(on_grid, w // 2, mode="edge"), np.ones(w) / w, mode="valid")
    out: FloatArray = np.interp(d, grid, smooth).astype(np.float64)
    return out


def xy_align(target: CleanLap, reference: CleanLap, *, weight: float | None = None,
             smooth_m: float | None = None) -> CleanLap:
    """Correct the (length-normalized) target's distance with its X/Y position; see module doc.
    weight blends the two independent estimates: 0 = distance only, 1 = X/Y only."""
    weight = XY_WEIGHT if weight is None else weight
    smooth_m = XY_SMOOTH_M if smooth_m is None else smooth_m
    rx, ry = (np.asarray(a, dtype=np.float64) / XY_UNITS_PER_M for a in (reference.x, reference.y))
    tx, ty = (np.asarray(a, dtype=np.float64) / XY_UNITS_PER_M for a in (target.x, target.y))
    label = f"{target.driver or '?'} lap {target.lap_number}"
    finite = np.isfinite(tx) & np.isfinite(ty)
    if not (np.isfinite(rx).all() and np.isfinite(ry).all()) or finite.mean() < XY_MIN_VALID_FRACTION:
        log.info("alignment.xy_fallback", lap=label, reason="missing_xy")
        return replace(target, alignment="distance")
    d = target.distance_m
    arc, perp = _project(rx, ry, reference.distance_m, np.where(finite, tx, 0.0), np.where(finite, ty, 0.0), d)
    offset = arc - d
    good = finite & (perp <= XY_MAX_PERP_M) & (np.abs(offset) <= XY_MAX_OFFSET_M)
    if good.mean() < XY_MIN_VALID_FRACTION:
        log.info("alignment.xy_fallback", lap=label, reason="projection_implausible", valid=float(good.mean()))
        return replace(target, alignment="distance")
    good[0] = good[-1] = True
    offset[0] = offset[-1] = 0.0                       # the timing line: start and finish are exact
    offset = np.interp(d, d[good], offset[good]).astype(np.float64)  # untrusted samples take their neighbours' offset
    taper = np.minimum(1.0, np.minimum(d, d[-1] - d) / smooth_m)  # reach 0 exactly at both lines
    corrected = np.maximum.accumulate(np.clip(d + weight * _lowpass(d, offset, smooth_m) * taper, 0.0, d[-1]))
    corrected, _ = _strictly_increasing(corrected)
    corrected = corrected * (d[-1] / corrected[-1])     # exact lap length after any epsilon nudge
    corrected[0], corrected[-1] = 0.0, d[-1]
    applied = corrected - d
    return replace(target, distance_m=corrected.astype(np.float64), alignment="xy",
                   xy_correction_rms_m=float(np.sqrt(np.mean(applied ** 2))),
                   xy_correction_max_m=float(np.max(np.abs(applied))))


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
