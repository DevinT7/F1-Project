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


def sector_deltas_from_trace(points: list[DeltaPoint], n_sectors: int = 3) -> list[SectorDelta]:
    """Fallback sector deltas derived from the delta trace itself, for when the
    `laps` rows don't carry real `duration_sector_*` fields (or one driver's is
    missing). Splits the shared distance into `n_sectors` equal-distance chunks
    (not real circuit sector boundaries -- just an even split) and reads the
    delta-trace change across each chunk, using the same implicit zero-at-start-
    of-lap baseline as `compare_traces`' time_gained_s/time_lost_s split."""
    if not points:
        return []
    total_distance = points[-1].distance
    if total_distance <= 0:
        return []
    xs = [0.0] + [p.distance for p in points]
    ys = [0.0] + [p.delta_s for p in points]
    edges = [total_distance * k / n_sectors for k in range(n_sectors + 1)]
    out: list[SectorDelta] = []
    for i in range(n_sectors):
        d_lo = float(np.interp(edges[i], xs, ys))
        d_hi = float(np.interp(edges[i + 1], xs, ys))
        out.append(SectorDelta(sector=i + 1, delta_s=round(d_hi - d_lo, 3)))
    return out


# Distance-based (not grid-index-based) windows so behaviour doesn't silently
# change if `step_m` is tuned: entry/min/exit speed and the time-delta window
# look +/- this far around the apex, while the throttle-delay search gets a
# much longer post-apex leash since cars often take 150-250m to reach full
# throttle after a corner -- the old fixed +/-8-grid-point window (40m at the
# default step_m=5) almost never contained the full-throttle point at all.
CORNER_WINDOW_M = 60.0
THROTTLE_SEARCH_M = 250.0


async def get_known_corners(meta: tuple[int, str, str] | None) -> list[dict] | None:
    """Best-effort FastF1 circuit-corner lookup for `detect_corners`' preferred
    `known` corner source. Returns `None` (never raises) on any failure --
    fastf1 not installed, circuit info unavailable for this session, network
    hiccup -- so the caller falls back to the speed-minima heuristic instead of
    erroring the whole comparison."""
    if meta is None:
        return None
    year, gp, session_name = meta
    try:
        from ..clients import fastf1_client
        corners = await fastf1_client.circuit_corners(year, gp, session_name)
        return corners or None
    except Exception:
        return None


def detect_corners(driver: TelemetryTrace, reference: TelemetryTrace,
                   known: list[dict] | None = None, step_m: float = 5.0) -> list[CornerInsight]:
    """
    Corner insights. If `known` corner distances are supplied (from FastF1 circuit
    info via `get_known_corners`) use them; otherwise fall back to detecting local
    speed minima.
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
        half_idx = max(1, round(CORNER_WINDOW_M / step_m))
        centres = [(n + 1, None, idx) for n, idx in enumerate(_speed_minima(speed_a, half=half_idx))]

    seg_a = _segment_times(speed_a, step_m)
    seg_b = _segment_times(db["speed"], step_m)

    half = max(1, round(CORNER_WINDOW_M / step_m))
    throttle_half = max(half, round(THROTTLE_SEARCH_M / step_m))

    insights: list[CornerInsight] = []
    for corner_number, name, idx in centres:
        lo, hi = max(0, idx - half), min(len(grid) - 1, idx + half)
        hi_throttle = min(len(grid) - 1, idx + throttle_half)
        win = slice(lo, hi + 1)
        min_speed = float(np.nanmin(speed_a[win]))
        entry_speed = float(speed_a[lo])
        exit_speed = float(speed_a[hi])
        braking_pt = _braking_point(grid, speed_a, lo, idx)
        throttle_delay = _throttle_delay(grid, thr_a, db["throttle"], idx, hi_throttle,
                                         apex_speed_kmh=min_speed)
        time_delta = float(np.nansum(seg_a[win]) - np.nansum(seg_b[win]))

        # Reference-side braking point / min speed, purely to derive the two
        # vs-reference deltas below -- `driver`'s own absolute values above are
        # what P4 plots; these two are what the AI narrative reads (see the
        # consumer-field doc block on CornerInsight in schemas.py).
        min_speed_ref = float(np.nanmin(db["speed"][win]))
        braking_pt_ref = _braking_point(grid, db["speed"], lo, idx)
        braking_delta = (round(braking_pt - braking_pt_ref, 1)
                        if braking_pt is not None and braking_pt_ref is not None else None)
        min_speed_delta = round(min_speed - min_speed_ref, 1)

        insights.append(CornerInsight(
            corner_number=corner_number, corner_name=name,
            distance=float(grid[idx]),
            entry_speed=round(entry_speed, 1), min_speed=round(min_speed, 1),
            exit_speed=round(exit_speed, 1),
            braking_point_m=braking_pt,
            braking_delta_m=braking_delta,
            throttle_delay_s=throttle_delay,
            min_speed_delta_kmh=min_speed_delta,
            time_delta_s=round(time_delta, 3),
        ))
    return insights


def compare_traces(driver_meta: Driver, ref_meta: Driver,
                   driver: TelemetryTrace, reference: TelemetryTrace,
                   driver_lap: dict | None = None, ref_lap: dict | None = None,
                   corners_known: list[dict] | None = None,
                   include_traces: bool = True) -> LapComparison:
    points, total = delta_trace(driver, reference)
    # Prepend an implicit (distance=0, delta=0) baseline -- the lap starts level --
    # so the monotonic-piece split below reconciles exactly with total_delta_s
    # (points[0].delta_s is already nonzero: it's the delta over the *first*
    # distance segment, not a true zero starting point).
    deltas_from_start = np.diff([0.0] + [p.delta_s for p in points])
    gained = float(np.sum(np.clip(deltas_from_start, None, 0.0)))
    lost = float(np.sum(np.clip(deltas_from_start, 0.0, None)))
    sectors = sector_deltas_from_laps(driver_lap, ref_lap)
    if not sectors:
        sectors = sector_deltas_from_trace(points)
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
        warnings=_anomaly_warnings(driver_meta, ref_meta, driver, reference),
    )


def _anomaly_warnings(driver_meta: Driver, ref_meta: Driver,
                      driver: TelemetryTrace, reference: TelemetryTrace) -> list[str]:
    """Turn `is_anomalous_lap` flags into a plain-English caveat, so a
    traffic/safety-car-affected lap surfaces as an explicit warning instead of
    silently looking like a routine, trustworthy comparison."""
    warnings: list[str] = []
    if driver.is_anomalous_lap:
        who = driver_meta.name_acronym or f"driver {driver_meta.driver_number}"
        warnings.append(
            f"{who}'s lap {driver.lap_number} looks anomalous (much slower than "
            f"their other laps this session) -- possible traffic, safety car, or "
            f"red flag. Treat this comparison with caution.")
    if reference.is_anomalous_lap:
        who = ref_meta.name_acronym or f"driver {ref_meta.driver_number}"
        warnings.append(
            f"{who}'s lap {reference.lap_number} looks anomalous (much slower "
            f"than their other laps this session) -- possible traffic, safety "
            f"car, or red flag. Treat this comparison with caution.")
    return warnings


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


MIN_APEX_SPEED_MS = 5.0  # floor so a near-stationary hairpin can't blow up the division


def _throttle_delay(grid: np.ndarray, thr_a: np.ndarray, thr_b: np.ndarray,
                    apex: int, hi: int, *, apex_speed_kmh: float) -> float | None:
    """Extra time (s) for driver A to reach full throttle after apex vs reference B.

    The distance-to-full-throttle gap is converted to a time using the corner's
    own apex/min speed (clamped away from zero), not a fixed constant -- a
    hairpin exiting at ~80 km/h and a fast kink exiting at ~250 km/h cover the
    same distance gap in very different times, so a single hardcoded speed
    over- or under-estimates the delay depending on the corner.
    """
    def dist_to_full(thr: np.ndarray) -> float | None:
        for j in range(apex, hi + 1):
            if thr[j] >= 95:
                return grid[j]
        return None
    da, dbb = dist_to_full(thr_a), dist_to_full(thr_b)
    if da is None or dbb is None:
        return None
    v_ms = max(apex_speed_kmh * (1000.0 / 3600.0), MIN_APEX_SPEED_MS)
    return round((da - dbb) / v_ms, 3)
