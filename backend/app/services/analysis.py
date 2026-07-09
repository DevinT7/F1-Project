"""
Lap comparison / corner analysis engine (Person 2).

Given two distance-aligned telemetry traces, compute:
  - a cumulative time-delta trace (where time is gained/lost around the lap),
  - sector deltas,
  - per-corner insights (entry/min/exit speed, braking point, throttle delay).

Time delta by distance is derived from the speed traces: over each small distance
segment, time = distance / speed. The running difference of those times is the delta.
"""
from __future__ import annotations

import numpy as np

from ..models.schemas import (
    CornerInsight, DeltaPoint, Driver, LapComparison, SectorDelta, TelemetryTrace,
)
from . import telemetry as tel


def _segment_times(speed_kmh: np.ndarray, step_m: float) -> np.ndarray:
    """Seconds to cover each `step_m` segment given speed (km/h). Guards against 0."""
    v = np.clip(speed_kmh * (1000.0 / 3600.0), 0.1, None)  # m/s
    return step_m / v


def delta_trace(driver: TelemetryTrace, reference: TelemetryTrace,
                step_m: float = 10.0) -> tuple[list[DeltaPoint], float]:
    grid = tel.common_grid(driver, reference, step_m=step_m)
    a = tel.resample_to_grid(driver, grid)["speed"]
    b = tel.resample_to_grid(reference, grid)["speed"]
    ta = np.nancumsum(_segment_times(a, step_m))
    tb = np.nancumsum(_segment_times(b, step_m))
    delta = ta - tb  # negative => driver ahead
    points = [DeltaPoint(distance=float(g), delta_s=float(d)) for g, d in zip(grid, delta)]
    total = float(delta[-1]) if len(delta) else 0.0
    return points, total


def sector_deltas_from_laps(driver_lap: dict | None, ref_lap: dict | None) -> list[SectorDelta]:
    """Prefer real sector times from the OpenF1 `laps` row when available."""
    out: list[SectorDelta] = []
    if not driver_lap or not ref_lap:
        return out
    for i in (1, 2, 3):
        dv = driver_lap.get(f"duration_sector_{i}")
        rv = ref_lap.get(f"duration_sector_{i}")
        if dv is not None and rv is not None:
            out.append(SectorDelta(sector=i, delta_s=round(float(dv) - float(rv), 3)))
    return out


def detect_corners(driver: TelemetryTrace, reference: TelemetryTrace,
                   known: list[dict] | None = None, step_m: float = 5.0) -> list[CornerInsight]:
    """
    Corner insights. If `known` corner distances are supplied (from FastF1 circuit
    info) use them; otherwise fall back to detecting local speed minima.
    """
    grid = tel.common_grid(driver, reference, step_m=step_m)
    if len(grid) < 5:
        return []
    da = tel.resample_to_grid(driver, grid)
    db = tel.resample_to_grid(reference, grid)
    speed_a, thr_a = da["speed"], da["throttle"]

    if known:
        centres = [(c["corner_number"], c.get("corner_name"),
                    _nearest_idx(grid, c["distance"])) for c in known]
    else:
        centres = [(n + 1, None, idx) for n, idx in enumerate(_speed_minima(speed_a))]

    seg_a = _segment_times(speed_a, step_m)
    seg_b = _segment_times(db["speed"], step_m)

    insights: list[CornerInsight] = []
    for corner_number, name, idx in centres:
        lo, hi = max(0, idx - 8), min(len(grid) - 1, idx + 8)
        win = slice(lo, hi + 1)
        min_speed = float(np.nanmin(speed_a[win]))
        entry_speed = float(speed_a[lo])
        exit_speed = float(speed_a[hi])
        braking_pt = _braking_point(grid, speed_a, lo, idx)
        throttle_delay = _throttle_delay(grid, thr_a, db["throttle"], idx, hi, step_m)
        time_delta = float(np.nansum(seg_a[win]) - np.nansum(seg_b[win]))
        insights.append(CornerInsight(
            corner_number=corner_number, corner_name=name,
            distance=float(grid[idx]),
            entry_speed=round(entry_speed, 1), min_speed=round(min_speed, 1),
            exit_speed=round(exit_speed, 1),
            braking_point_m=braking_pt,
            throttle_delay_s=throttle_delay,
            time_delta_s=round(time_delta, 3),
        ))
    return insights


def compare_traces(driver_meta: Driver, ref_meta: Driver,
                   driver: TelemetryTrace, reference: TelemetryTrace,
                   driver_lap: dict | None = None, ref_lap: dict | None = None,
                   corners_known: list[dict] | None = None,
                   include_traces: bool = True) -> LapComparison:
    points, total = delta_trace(driver, reference)
    gained = sum(min(0.0, points[i].delta_s - points[i - 1].delta_s)
                 for i in range(1, len(points)))
    lost = sum(max(0.0, points[i].delta_s - points[i - 1].delta_s)
               for i in range(1, len(points)))
    sectors = sector_deltas_from_laps(driver_lap, ref_lap)
    corners = detect_corners(driver, reference, known=corners_known)
    return LapComparison(
        session_key=driver.session_key,
        driver=driver_meta, reference=ref_meta,
        lap_time=driver.lap_time, reference_lap_time=reference.lap_time,
        total_delta_s=round(total, 3),
        time_gained_s=round(gained, 3), time_lost_s=round(lost, 3),
        sector_deltas=sectors, delta_trace=points, corners=corners,
        driver_trace=driver if include_traces else None,
        reference_trace=reference if include_traces else None,
    )


# ---- helpers ----
def _nearest_idx(grid: np.ndarray, distance: float) -> int:
    return int(np.argmin(np.abs(grid - distance)))


def _speed_minima(speed: np.ndarray, prominence: float = 20.0, half: int = 12) -> list[int]:
    """Local speed minima with prominence measured over a wide (+/- `half`) window,
    so broad braking valleys are detected, not just sharp spikes."""
    idxs: list[int] = []
    n = len(speed)
    for i in range(half, n - half):
        window = speed[i - half:i + half + 1]
        if speed[i] <= np.nanmin(window) and (np.nanmax(window) - speed[i]) > prominence:
            if not idxs or i - idxs[-1] > half:
                idxs.append(i)
    return idxs


def _braking_point(grid: np.ndarray, speed: np.ndarray, lo: int, apex: int) -> float | None:
    """Distance where the car starts decelerating hardest before the apex."""
    if apex <= lo:
        return None
    d_speed = np.diff(speed[lo:apex + 1])
    if len(d_speed) == 0:
        return None
    k = int(np.argmin(d_speed))  # most negative slope
    return round(float(grid[lo + k]), 1)


def _throttle_delay(grid: np.ndarray, thr_a: np.ndarray, thr_b: np.ndarray,
                    apex: int, hi: int, step_m: float) -> float | None:
    """Extra time (s) for driver A to reach full throttle after apex vs reference B."""
    def dist_to_full(thr: np.ndarray) -> float | None:
        for j in range(apex, hi + 1):
            if thr[j] >= 95:
                return grid[j]
        return None
    da, dbb = dist_to_full(thr_a), dist_to_full(thr_b)
    if da is None or dbb is None:
        return None
    # convert the distance difference to an approximate time using ~apex speed
    return round((da - dbb) / max(30.0, 1.0), 3)
