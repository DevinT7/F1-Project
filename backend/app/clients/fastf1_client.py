"""
FastF1 client — https://github.com/theOehrly/Fast-F1  (Person 1, used by P2/P3).

FastF1 gives us the thing OpenF1 doesn't: telemetry already aligned to a **Distance**
channel, plus corner/circuit info. It's synchronous and heavy, so we run it in a
thread and lean on its on-disk cache.

Import is lazy so the app boots even if fastf1 isn't installed yet. Enable/disable
via RACEIQ_ENABLE_FASTF1.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from ..config import settings
from ..models.schemas import TelemetrySample, TelemetryTrace

_initialised = False


def _ensure_cache() -> Any:
    """Import fastf1 lazily and enable its cache once."""
    global _initialised
    import fastf1  # noqa: WPS433  (lazy on purpose)

    if not _initialised:
        os.makedirs(settings.fastf1_cache_dir, exist_ok=True)
        fastf1.Cache.enable_cache(settings.fastf1_cache_dir)
        _initialised = True
    return fastf1


def _fastest_lap_trace_sync(year: int, gp: str, session: str,
                            driver: str) -> TelemetryTrace:
    """Blocking FastF1 work — call via asyncio.to_thread."""
    fastf1 = _ensure_cache()
    ses = fastf1.get_session(year, gp, session)
    ses.load(telemetry=True, laps=True, weather=False)
    lap = ses.laps.pick_drivers(driver).pick_fastest()
    tel = lap.get_car_data().add_distance()

    samples: list[TelemetrySample] = []
    for _, row in tel.iterrows():
        samples.append(TelemetrySample(
            distance=float(row["Distance"]),
            speed=_f(row.get("Speed")),
            throttle=_f(row.get("Throttle")),
            brake=100.0 if bool(row.get("Brake")) else 0.0,
            n_gear=_i(row.get("nGear")),
            rpm=_f(row.get("RPM")),
            drs=_i(row.get("DRS")),
        ))

    lap_time = None
    if lap["LapTime"] is not None and not _is_nat(lap["LapTime"]):
        lap_time = lap["LapTime"].total_seconds()

    return TelemetryTrace(
        session_key=int(getattr(ses, "session_key", 0) or 0),
        driver_number=int(lap["DriverNumber"]),
        lap_number=_i(lap["LapNumber"]),
        lap_time=lap_time,
        samples=samples,
    )


async def fastest_lap_trace(year: int, gp: str, session: str, driver: str) -> TelemetryTrace:
    return await asyncio.to_thread(_fastest_lap_trace_sync, year, gp, session, driver)


def _corners_sync(year: int, gp: str, session: str) -> list[dict]:
    fastf1 = _ensure_cache()
    ses = fastf1.get_session(year, gp, session)
    ses.load(telemetry=False, laps=False, weather=False)
    ci = ses.get_circuit_info()
    return [
        {"corner_number": int(r["Number"]), "distance": float(r["Distance"]),
         "angle": float(r.get("Angle", 0.0))}
        for _, r in ci.corners.iterrows()
    ]


async def circuit_corners(year: int, gp: str, session: str) -> list[dict]:
    return await asyncio.to_thread(_corners_sync, year, gp, session)


# ---- small coercion helpers (FastF1 rows contain numpy/NaT) ----
def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _is_nat(v: Any) -> bool:
    return str(v) == "NaT"
