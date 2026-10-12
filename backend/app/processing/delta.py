"""Cumulative time delta between two aligned laps, plus sector cross-checks.

The delta comes from the recorded timestamps (elapsed time at each grid distance),
never from integrating 1/speed. Its final value must reproduce the official lap-time
gap within ±0.02 s or the comparison is refused with InsufficientDataError: a wrong
number is worse than no number.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import savgol_filter

from ..ingestion.models import FloatArray
from .alignment import clean_lap, make_grid, normalize_length, resample_to_grid
from .errors import InsufficientDataError
from .models import (
    AlignedLap,
    ComparisonQuality,
    LapComparison,
    LapData,
    SectorCheck,
    SectorDiagnostic,
)

FINAL_DELTA_TOLERANCE_S = 0.02
SECTOR_TOLERANCE_S = 0.03
SMOOTH_WINDOW_M = 25.0
SMOOTH_POLYORDER = 2


def compute_delta(reference: AlignedLap, target: AlignedLap) -> FloatArray:
    """delta_s(d) = t_target(d) − t_reference(d); positive means the target is behind."""
    if len(reference.time_s) != len(target.time_s):
        raise ValueError("laps are not on the same grid")
    for lap in (reference, target):
        if not np.all(np.diff(lap.time_s) > 0):
            raise InsufficientDataError(
                f"{lap.driver} lap {lap.lap_number}: elapsed time is not strictly increasing on the grid."
            )
    delta: FloatArray = (target.time_s - reference.time_s).astype(np.float64)
    delta[0] = 0.0
    return delta


def smooth_rate(rate: FloatArray, step_m: float) -> FloatArray:
    """Savitzky–Golay over ~25 m. Display only — never feed this into time-loss numbers."""
    window = int(round(SMOOTH_WINDOW_M / step_m)) | 1  # odd number of points
    if window <= SMOOTH_POLYORDER or window > len(rate):
        return rate.copy()
    smoothed: FloatArray = savgol_filter(rate, window, SMOOTH_POLYORDER, mode="interp")
    return smoothed


def compare_laps(reference: LapData, target: LapData, step: float = 1.0) -> LapComparison:
    ref_clean = clean_lap(reference)
    tgt_clean = normalize_length(clean_lap(target), ref_clean.length_m)
    grid = make_grid(ref_clean.length_m, step)
    ref = resample_to_grid(ref_clean, grid)
    tgt = resample_to_grid(tgt_clean, grid)

    delta = compute_delta(ref, tgt)
    expected = target.lap_time_s - reference.lap_time_s
    error = float(delta[-1] - expected)
    if abs(error) > FINAL_DELTA_TOLERANCE_S:
        raise InsufficientDataError(
            f"final delta {delta[-1]:+.3f}s does not match the official gap {expected:+.3f}s "
            f"(discrepancy {error:+.3f}s > ±{FINAL_DELTA_TOLERANCE_S}s) for "
            f"{target.driver} lap {target.lap_number} vs {reference.driver} lap {reference.lap_number}."
        )

    rate = np.gradient(delta, grid)
    return LapComparison(
        grid_m=grid,
        reference=ref,
        target=tgt,
        delta_s=delta,
        delta_rate=rate,
        delta_rate_smoothed=smooth_rate(rate, step),
        speed_diff_kph=(tgt.speed_kph - ref.speed_kph).astype(np.float64),
        quality=ComparisonQuality(
            sample_count_ref=ref_clean.sample_count,
            sample_count_target=tgt_clean.sample_count,
            length_scale_factor=tgt_clean.length_scale_factor,
            final_delta_error_s=error,
            repairs=ref_clean.repairs + tgt_clean.repairs,
            dropped_glitches=ref_clean.dropped_glitches + tgt_clean.dropped_glitches,
        ),
    )


def sector_check(comparison: LapComparison, tolerance_s: float = SECTOR_TOLERANCE_S) -> SectorDiagnostic:
    """Independent check: official sector-time gaps vs the grid delta at sector boundaries.

    Boundary distances are estimated from the reference lap: where its elapsed time
    t_ref(d) crosses S1 and S1+S2. The finish line (sector 3) is checked too.
    """
    ref_s, tgt_s = comparison.reference.sector_s, comparison.target.sector_s
    if any(v is None for v in (*ref_s, *tgt_s)):
        return SectorDiagnostic(False, (None, None, None), (), tolerance_s)
    ref = [float(v) for v in ref_s if v is not None]
    tgt = [float(v) for v in tgt_s if v is not None]
    sector_deltas = (tgt[0] - ref[0], tgt[1] - ref[1], tgt[2] - ref[2])

    grid, delta = comparison.grid_m, comparison.delta_s
    t_ref = comparison.reference.time_s
    checks: list[SectorCheck] = []
    for sector in (1, 2, 3):
        official = float(sum(sector_deltas[:sector]))
        if sector == 3:
            boundary, grid_delta = float(grid[-1]), float(delta[-1])
        else:
            boundary = float(np.interp(sum(ref[:sector]), t_ref, grid))
            grid_delta = float(np.interp(boundary, grid, delta))
        discrepancy = grid_delta - official
        checks.append(SectorCheck(sector, boundary, official, grid_delta, discrepancy,
                                  abs(discrepancy) <= tolerance_s))
    return SectorDiagnostic(True, sector_deltas, tuple(checks), tolerance_s)
