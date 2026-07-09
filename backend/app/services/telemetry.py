"""
Telemetry shaping (Person 2).

OpenF1 `car_data` is time-sampled, not distance-indexed. To satisfy the project's
"align both laps by distance" requirement we:
  1. find the lap's time window from the `laps` endpoint,
  2. pull `car_data` in that window,
  3. derive cumulative distance by integrating speed over time,
  4. resample onto a common distance grid so two laps line up point-for-point.

For richer/older data you can swap in fastf1_client.fastest_lap_trace() which already
provides a Distance channel — same output shape.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import numpy as np

from ..clients.openf1 import get_client
from ..models.schemas import TelemetrySample, TelemetryTrace


MIN_USABLE_SAMPLES = 2       # can't derive a distance channel from fewer points
MIN_USABLE_DISTANCE_M = 1.0  # a trace that never moves is not a usable lap

# Python's `datetime.fromisoformat` (< 3.11) only accepts 0, 3, or 6 fractional-
# second digits. OpenF1 usually emits 6, but pad/truncate defensively so any
# variant (or a hand-built fixture/mock) still parses instead of silently
# dropping the sample.
_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d+)")


def _normalise_fractional_seconds(dt: str) -> str:
    def _pad(match: re.Match) -> str:
        digits = match.group(1)[:6].ljust(6, "0")
        return f".{digits}"
    return _FRACTIONAL_SECONDS_RE.sub(_pad, dt, count=1)


def _parse(dt: str | None) -> datetime | None:
    if not dt:
        return None
    try:
        return datetime.fromisoformat(_normalise_fractional_seconds(dt.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _empty_trace(session_key: int | str, driver_number: int,
                 lap_number: int | None, lap_time: float | None = None) -> TelemetryTrace:
    return TelemetryTrace(session_key=int(session_key), driver_number=driver_number,
                          lap_number=lap_number, lap_time=lap_time, samples=[])


ANOMALY_MAD_MULTIPLIER = 3.0        # statistical-outlier threshold (MAD units)
ANOMALY_MIN_SLOWER_RATIO = 1.10     # ...and at least 10% slower than the field's median


def _is_anomalous_lap_duration(candidate: float, other_durations: list[float]) -> bool:
    """Flag a lap whose duration looks like traffic/a safety car/VSC/red flag --
    both a statistical outlier vs. the driver's other laps this session
    (MAD-based, mirrors tires.py's `_remove_outliers`) AND meaningfully slower
    than the field. The ratio guard means a driver's only lap (no `others`), or
    a session where every lap is naturally close together, never gets flagged
    on noise alone -- this is a coarse traffic/SC signal, not a lap-time model.
    """
    if not other_durations:
        return False
    arr = np.array(other_durations, dtype=float)
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med))) or 0.05
    return candidate > med + ANOMALY_MAD_MULTIPLIER * mad and candidate > med * ANOMALY_MIN_SLOWER_RATIO


async def _lap_window(session_key: int | str, driver_number: int,
                      lap_number: int | None) -> tuple[str | None, str | None, dict | None, bool]:
    """Return (date_start, date_end, lap_row, is_anomalous). If lap_number is
    None, use fastest lap.

    Filters out pit-out laps and laps with a missing/unparsable duration or
    start time, since none of those give us a clean, comparable telemetry
    window. `is_anomalous` flags (but never silently swaps out) a lap whose
    duration stands out from the driver's other laps this session -- likely
    traffic/safety-car/red-flag affected -- so callers can warn rather than
    present a contaminated comparison as routine.
    """
    laps = await get_client().laps(session_key=session_key, driver_number=driver_number)

    def _valid(l: dict) -> bool:
        if l.get("is_pit_out_lap"):
            return False
        try:
            return float(l.get("lap_duration")) > 0
        except (TypeError, ValueError):
            return False

    laps = [l for l in laps if _valid(l)]
    if not laps:
        return None, None, None, False
    if lap_number is not None:
        lap = next((l for l in laps if l.get("lap_number") == lap_number), None)
    else:
        lap = min(laps, key=lambda l: float(l["lap_duration"]))
    if not lap:
        return None, None, None, False
    is_anomalous = _is_anomalous_lap_duration(
        float(lap["lap_duration"]), [float(l["lap_duration"]) for l in laps if l is not lap])
    if not lap.get("date_start"):
        return None, None, lap, is_anomalous
    start = _parse(lap["date_start"])
    if start is None:
        return None, None, lap, is_anomalous
    end = start + timedelta(seconds=float(lap["lap_duration"]) + 1.0)
    return start.isoformat(), end.isoformat(), lap, is_anomalous


async def get_lap_row(session_key: int | str, driver_number: int,
                      lap_number: int | None) -> dict | None:
    """Fetch the raw OpenF1 `laps` row for a specific, already-known lap number.

    Used by the router to hand `analysis.compare_traces` the real sector-duration
    fields for the exact lap a trace was built from (pass the trace's own
    `lap_number`, not the caller's original possibly-`None` request param, so the
    row always matches the telemetry that was actually used). Never raises --
    returns `None` on any lookup failure so sector deltas simply fall back to the
    delta-trace-derived approximation instead of breaking the whole comparison.
    """
    if lap_number is None:
        return None
    try:
        laps = await get_client().laps(session_key=session_key, driver_number=driver_number,
                                       lap_number=lap_number)
    except Exception:
        return None
    return next((l for l in laps if l.get("lap_number") == lap_number), None)


async def build_trace(session_key: int | str, driver_number: int,
                      lap_number: int | None = None,
                      *, prefer_fastf1: bool = False,
                      fastf1_meta: tuple[int, str, str, str] | None = None) -> TelemetryTrace:
    """Build a distance-indexed telemetry trace for one lap.

    Never raises on bad/missing/degenerate data — every failure mode collapses to
    an empty-samples `TelemetryTrace` so callers can uniformly treat "no usable
    telemetry" as a 404 rather than handling N different exceptions.

    If `prefer_fastf1` is set and `fastf1_meta` (year, gp, session_name, driver_code)
    is supplied, try FastF1's native `Distance` channel first (useful for older
    seasons or when OpenF1's speed-integration proxy isn't precise enough); on any
    failure or empty result, fall back to the OpenF1 integration path below.
    """
    if prefer_fastf1 and fastf1_meta is not None:
        trace = await _try_fastf1_trace(fastf1_meta)
        if trace is not None and trace.samples:
            return trace
        # fall through to OpenF1 path on failure/empty result

    date_gt, date_lt, lap, is_anomalous = await _lap_window(session_key, driver_number, lap_number)
    if date_gt is None:
        return _empty_trace(session_key, driver_number, lap_number,
                            lap.get("lap_duration") if lap else None)

    rows = await get_client().car_data(
        session_key=session_key, driver_number=driver_number,
        date_gt=date_gt, date_lt=date_lt)
    # keep only rows with a parsable date; sort defensively (OpenF1 usually returns
    # date-ordered rows already, but don't assume it for a fixture / mirror / cache)
    parsed = [(_parse(r.get("date")), r) for r in rows]
    parsed = [(t, r) for t, r in parsed if t is not None]
    parsed.sort(key=lambda tr: tr[0])

    samples: list[TelemetrySample] = []
    dist = 0.0
    prev_t: datetime | None = None
    for t, r in parsed:
        speed = _num(r.get("speed"))  # km/h; None/NaN-safe
        if prev_t is not None and speed is not None and speed > 0:
            dt = (t - prev_t).total_seconds()
            if dt > 0:  # guard against duplicate/out-of-order timestamps
                dist += speed * (1000.0 / 3600.0) * dt  # metres
        prev_t = t
        samples.append(TelemetrySample(
            distance=round(dist, 2),
            speed=speed,
            throttle=_num(r.get("throttle")),
            brake=_num(r.get("brake")),
            n_gear=_int(r.get("n_gear")),
            rpm=_num(r.get("rpm")),
            drs=_int(r.get("drs")),
        ))

    lap_time = lap.get("lap_duration") if lap else None
    lap_num_out = lap.get("lap_number") if lap else lap_number

    # degenerate cases: too few points, or the car "never moves" (e.g. every speed
    # sample was missing/zero) -> nothing usable to align against, treat as empty
    if len(samples) < MIN_USABLE_SAMPLES or samples[-1].distance < MIN_USABLE_DISTANCE_M:
        return _empty_trace(session_key, driver_number, lap_num_out, lap_time)

    return TelemetryTrace(
        session_key=int(session_key),
        driver_number=driver_number,
        lap_number=lap_num_out,
        lap_time=lap_time,
        is_anomalous_lap=is_anomalous,
        samples=samples,
    )


async def _try_fastf1_trace(meta: tuple[int, str, str, str]) -> TelemetryTrace | None:
    """Best-effort FastF1 lookup. Returns None (never raises) on any failure so the
    caller can silently fall back to the OpenF1 path."""
    year, gp, session_name, driver_code = meta
    try:
        from ..clients import fastf1_client
        return await fastf1_client.fastest_lap_trace(year, gp, session_name, driver_code)
    except Exception:
        return None


def resample_to_grid(trace: TelemetryTrace, grid: np.ndarray) -> dict[str, np.ndarray]:
    """Interpolate a trace's channels onto a shared distance grid."""
    if not trace.samples:
        empty = np.full_like(grid, np.nan, dtype=float)
        return {k: empty.copy() for k in ("speed", "throttle", "brake", "n_gear", "rpm")}
    d = np.array([s.distance for s in trace.samples], dtype=float)
    out: dict[str, np.ndarray] = {}
    for chan in ("speed", "throttle", "brake", "n_gear", "rpm"):
        y = np.array([getattr(s, chan) if getattr(s, chan) is not None else np.nan
                      for s in trace.samples], dtype=float)
        out[chan] = np.interp(grid, d, y)
    return out


def common_grid(*traces: TelemetryTrace, step_m: float = 10.0) -> np.ndarray:
    """A distance grid spanning the shortest common lap length.

    Returns a single-point grid (rather than an empty/negative-length array) when
    traces are missing or too short to share any common distance span, so callers
    never have to special-case a zero-length np.arange.
    """
    lengths = [t.samples[-1].distance for t in traces if t.samples]
    if not lengths:
        return np.array([0.0])
    end = min(lengths)
    if end < step_m:
        return np.array([0.0])
    return np.arange(0.0, end, step_m)


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if f != f else f  # NaN check (NaN != NaN)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
