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

from datetime import datetime, timedelta

import numpy as np

from ..clients.openf1 import get_client
from ..models.schemas import TelemetrySample, TelemetryTrace


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


async def _lap_window(session_key: int | str, driver_number: int,
                      lap_number: int | None) -> tuple[str | None, str | None, dict | None]:
    """Return (date_start, date_end, lap_row). If lap_number is None, use fastest lap."""
    laps = await get_client().laps(session_key=session_key, driver_number=driver_number)
    laps = [l for l in laps if l.get("lap_duration") and not l.get("is_pit_out_lap")]
    if not laps:
        return None, None, None
    if lap_number is not None:
        lap = next((l for l in laps if l.get("lap_number") == lap_number), None)
    else:
        lap = min(laps, key=lambda l: l["lap_duration"])
    if not lap or not lap.get("date_start"):
        return None, None, lap
    start = _parse(lap["date_start"])
    end = start + timedelta(seconds=float(lap["lap_duration"]) + 1.0)
    return start.isoformat(), end.isoformat(), lap


async def build_trace(session_key: int | str, driver_number: int,
                      lap_number: int | None = None) -> TelemetryTrace:
    date_gt, date_lt, lap = await _lap_window(session_key, driver_number, lap_number)
    if date_gt is None:
        return TelemetryTrace(session_key=int(session_key), driver_number=driver_number,
                              lap_number=lap_number, samples=[])

    rows = await get_client().car_data(
        session_key=session_key, driver_number=driver_number,
        date_gt=date_gt, date_lt=date_lt)
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=lambda r: r["date"])

    samples: list[TelemetrySample] = []
    dist = 0.0
    prev_t: datetime | None = None
    for r in rows:
        t = _parse(r["date"])
        speed = r.get("speed")  # km/h
        if prev_t is not None and speed is not None:
            dt = (t - prev_t).total_seconds()
            dist += speed * (1000.0 / 3600.0) * dt  # metres
        prev_t = t
        samples.append(TelemetrySample(
            distance=round(dist, 2),
            speed=_num(r.get("speed")),
            throttle=_num(r.get("throttle")),
            brake=_num(r.get("brake")),
            n_gear=_int(r.get("n_gear")),
            rpm=_num(r.get("rpm")),
            drs=_int(r.get("drs")),
        ))

    return TelemetryTrace(
        session_key=int(session_key),
        driver_number=driver_number,
        lap_number=lap.get("lap_number") if lap else lap_number,
        lap_time=lap.get("lap_duration") if lap else None,
        samples=samples,
    )


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
    """A distance grid spanning the shortest common lap length."""
    lengths = [t.samples[-1].distance for t in traces if t.samples]
    if not lengths:
        return np.array([0.0])
    end = min(lengths)
    return np.arange(0.0, end, step_m)


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
