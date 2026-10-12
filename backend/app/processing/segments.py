"""Corner, straight and sector segments on a LapComparison's distance grid.

Corner segments tile the whole lap: apexes come from official corners matched to
speed minima of the reference lap, and boundaries sit at the distance midpoint
between consecutive apexes (first starts at 0, last ends at L_ref). Because they tile,
per-corner time losses sum exactly to the total delta.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
from scipy.signal import find_peaks, savgol_filter

from ..ingestion.models import CornerInfo, FloatArray
from .config import Thresholds
from .models import AlignedLap, LapComparison

ApexSource = Literal["matched", "official", "detected"]
CornerType = Literal["slow", "medium", "fast"]


@dataclass(frozen=True)
class CornerSegment:
    index: int
    label: str
    code: str                         # short id part: "T7", "T7A", "A4148"
    start_m: float
    end_m: float
    apex_m: float
    apex_source: ApexSource           # matched = official corner snapped to a detected minimum
    corner_number: int | None
    corner_letter: str | None
    official_distance_m: float | None
    xy_error_m: float | None          # official X/Y vs reference lap X/Y at the official distance
    xy_ok: bool


@dataclass(frozen=True)
class StraightSegment:
    index: int
    start_m: float
    end_m: float


def detect_apexes(grid_m: FloatArray, speed_kph: FloatArray, thresholds: Thresholds) -> FloatArray:
    """Distances of speed minima with prominence ≥ apex_prominence_kph, after light smoothing."""
    step = float(grid_m[1] - grid_m[0]) if len(grid_m) > 1 else 1.0
    window = max(int(round(thresholds.apex_smooth_m / step)) | 1, 3)
    speed = np.nan_to_num(speed_kph, nan=float(np.nanmax(speed_kph)) if np.isfinite(speed_kph).any() else 0.0)
    smooth = savgol_filter(speed, window, 2, mode="interp") if len(speed) > window else speed
    peaks, _ = find_peaks(-smooth, prominence=thresholds.apex_prominence_kph)
    out: FloatArray = grid_m[peaks].astype(np.float64)
    return out


def partition(apexes_m: Sequence[float], length_m: float) -> list[tuple[float, float]]:
    """Midpoint boundaries between sorted apexes; first starts at 0, last ends at length."""
    if not apexes_m:
        return [(0.0, float(length_m))]
    apexes = sorted(apexes_m)
    bounds = [0.0] + [(a + b) / 2.0 for a, b in zip(apexes, apexes[1:])] + [float(length_m)]
    return list(zip(bounds[:-1], bounds[1:]))


def corner_type(min_speed_kph: float | None, thresholds: Thresholds) -> CornerType | None:
    if min_speed_kph is None:
        return None
    if min_speed_kph < thresholds.slow_below_kph:
        return "slow"
    if min_speed_kph > thresholds.fast_above_kph:
        return "fast"
    return "medium"


def _xy_error_m(grid: FloatArray, lap: AlignedLap, corner: CornerInfo, thresholds: Thresholds) -> float | None:
    if not (0.0 <= corner.distance_m <= grid[-1]):
        return None
    x = float(np.interp(corner.distance_m, grid, lap.x))
    y = float(np.interp(corner.distance_m, grid, lap.y))
    if not (np.isfinite(x) and np.isfinite(y)):
        return None
    return float(np.hypot(x - corner.x, y - corner.y)) / thresholds.xy_units_per_m


def build_corner_segments(
    comparison: LapComparison,
    corners: Sequence[CornerInfo],
    thresholds: Thresholds = Thresholds(),
) -> list[CornerSegment]:
    grid, ref = comparison.grid_m, comparison.reference
    length = float(grid[-1])
    minima = [float(d) for d in detect_apexes(grid, ref.speed_kph, thresholds)]

    # Greedy one-to-one matching, closest pairs first, so two official corners cannot
    # claim the same minimum (chicanes) and the result does not depend on table order.
    official = [c for c in corners if 0.0 <= c.distance_m <= length]
    pairs = sorted(
        (abs(c.distance_m - m), ci, mi)
        for ci, c in enumerate(official)
        for mi, m in enumerate(minima)
        if abs(c.distance_m - m) <= thresholds.apex_match_m
    )
    corner_to_min: dict[int, int] = {}
    used: set[int] = set()
    for _, ci, mi in pairs:
        if ci not in corner_to_min and mi not in used:
            corner_to_min[ci] = mi
            used.add(mi)

    found: list[CornerSegment] = []
    for ci, corner in enumerate(official):
        xy_error = _xy_error_m(grid, ref, corner, thresholds)
        matched = ci in corner_to_min
        letter = corner.letter or None
        found.append(CornerSegment(
            index=0, start_m=0.0, end_m=0.0,
            apex_m=minima[corner_to_min[ci]] if matched else float(corner.distance_m),
            apex_source="matched" if matched else "official",
            label=f"Turn {corner.number}{letter or ''}",
            code=f"T{corner.number}{letter or ''}",
            corner_number=corner.number,
            corner_letter=letter,
            official_distance_m=float(corner.distance_m),
            xy_error_m=xy_error,
            xy_ok=xy_error is not None and xy_error <= thresholds.xy_tolerance_m,
        ))
    for mi, m in enumerate(minima):
        if mi not in used:
            found.append(CornerSegment(
                index=0, start_m=0.0, end_m=0.0, apex_m=m, apex_source="detected",
                label=f"Unnamed corner at {m:.0f} m", code=f"A{m:.0f}", corner_number=None,
                corner_letter=None, official_distance_m=None, xy_error_m=None, xy_ok=True,
            ))

    found.sort(key=lambda seg: (seg.apex_m, seg.code))
    spans = partition([seg.apex_m for seg in found], length)
    return [replace(seg, index=i, start_m=a, end_m=b) for i, (seg, (a, b)) in enumerate(zip(found, spans))]


def detect_straights(grid_m: FloatArray, lap: AlignedLap, thresholds: Thresholds) -> list[StraightSegment]:
    """Contiguous stretches ≥ straight_min_m where the lap is at ≥ straight_throttle_pct."""
    flat = np.nan_to_num(lap.throttle_pct, nan=0.0) >= thresholds.straight_throttle_pct
    edges = np.flatnonzero(np.diff(np.concatenate(([0], flat.astype(np.int8), [0]))))
    out: list[StraightSegment] = []
    for start, stop in zip(edges[::2], edges[1::2]):
        start_m, end_m = float(grid_m[start]), float(grid_m[stop - 1])
        if end_m - start_m >= thresholds.straight_min_m:
            out.append(StraightSegment(index=len(out), start_m=start_m, end_m=end_m))
    return out
