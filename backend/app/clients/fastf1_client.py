"""FastF1 client helpers for chart-ready telemetry."""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import threading
import time
from datetime import timedelta
from typing import Any, Iterable

import numpy as np

from .. import cache
from ..config import settings
from ..models.schemas import TelemetrySample, TelemetryTrace

logger = logging.getLogger(__name__)

SAMPLE_RATE_LIMITS = {
    "high": 2000,
    "medium": 1000,
    "low": 400,
}


class FastF1Error(RuntimeError):
    """Base class for normalized FastF1 errors."""


class FastF1DisabledError(FastF1Error):
    """FastF1 access is disabled by configuration."""


class FastF1SessionNotFoundError(FastF1Error):
    """The requested FastF1 session could not be loaded."""


class FastF1DriverNotFoundError(FastF1Error):
    """The requested driver was not present in the session laps."""


class FastF1LapNotFoundError(FastF1Error):
    """The requested lap number was not present for the driver."""


class FastF1TelemetryUnavailableError(FastF1Error):
    """FastF1 did not have usable telemetry for the selected lap."""


class FastF1TimeoutError(FastF1Error):
    """A FastF1 load exceeded the configured timeout."""


_initialised = False
_session_cache: dict[tuple[int, str, str], Any] = {}
_session_lock = threading.Lock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name) or os.getenv(f"RACEIQ_{name}")
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name) or os.getenv(f"RACEIQ_{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer env var %s=%r", name, raw)
        return default


def _fastf1_enabled() -> bool:
    return _env_bool("FASTF1_ENABLED", settings.enable_fastf1)


def _fastf1_cache_dir() -> str:
    return (
        os.getenv("FASTF1_CACHE_DIR")
        or os.getenv("RACEIQ_FASTF1_CACHE_DIR")
        or settings.fastf1_cache_dir
    )


def _fastf1_timeout_seconds() -> int:
    return _env_int("FASTF1_DEFAULT_TIMEOUT_SECONDS", 90)


def _telemetry_cache_ttl_seconds() -> int:
    return _env_int("TELEMETRY_CACHE_TTL_SECONDS", settings.cache_ttl_seconds)


def status() -> dict[str, Any]:
    enabled = _fastf1_enabled()
    return {
        "status": "ok" if enabled else "disabled",
        "enabled": enabled,
        "cache_dir": _fastf1_cache_dir(),
        "loaded_sessions": len(_session_cache),
    }


def _ensure_cache() -> Any:
    """Import FastF1 lazily and enable its on-disk cache once."""
    global _initialised
    if not _fastf1_enabled():
        raise FastF1DisabledError("FastF1 is disabled.")

    # Single FastF1 import site: the ingestion adapter (enables the cache once).
    from ..ingestion.fastf1_client import import_fastf1

    if not _initialised:
        cache_dir = _fastf1_cache_dir()
        fastf1 = import_fastf1(cache_dir)
        _initialised = True
        logger.info("FastF1 cache enabled at %s", cache_dir)
        return fastf1
    return import_fastf1()


def _session_key(year: int, grand_prix: str, session_type: str) -> tuple[int, str, str]:
    return (int(year), grand_prix.strip().lower(), session_type.strip().lower())


def _load_session_sync(year: int, grand_prix: str, session_type: str) -> Any:
    key = _session_key(year, grand_prix, session_type)
    with _session_lock:
        if key in _session_cache:
            return _session_cache[key]

        fastf1 = _ensure_cache()
        started = time.monotonic()
        try:
            session = fastf1.get_session(year, grand_prix, session_type)
            session.load(telemetry=True, laps=True, weather=False)
        except FastF1Error:
            raise
        except Exception as exc:
            logger.warning(
                "FastF1 failed to load %s %s %s: %s",
                year,
                grand_prix,
                session_type,
                exc,
            )
            raise FastF1SessionNotFoundError(
                f"FastF1 could not load {year} {grand_prix} {session_type}."
            ) from exc

        elapsed = time.monotonic() - started
        if elapsed > 10:
            logger.warning(
                "FastF1 cold-cache load took %.1fs for %s %s %s",
                elapsed,
                year,
                grand_prix,
                session_type,
            )
        else:
            logger.info(
                "FastF1 loaded %s %s %s in %.1fs",
                year,
                grand_prix,
                session_type,
                elapsed,
            )
        _session_cache[key] = session
        return session


async def _run_fastf1(func, *args):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args),
            timeout=_fastf1_timeout_seconds(),
        )
    except asyncio.TimeoutError as exc:
        logger.warning("FastF1 operation timed out after %ss", _fastf1_timeout_seconds())
        raise FastF1TimeoutError("FastF1 request timed out while loading telemetry.") from exc


class FastF1Client:
    def __init__(self, cache_backend: Any | None = None) -> None:
        self.cache = cache_backend or cache

    async def get_session(self, year: int, grand_prix: str, session_type: str) -> Any:
        return await _run_fastf1(_load_session_sync, year, grand_prix, session_type)

    async def get_laps(
        self,
        year: int,
        grand_prix: str,
        session_type: str,
        driver: str | None = None,
    ) -> dict[str, Any]:
        key = _cache_key("laps", year, grand_prix, session_type, driver or "all")
        cached = self.cache.get(key)
        if cached is not None:
            return _with_cached_meta(cached)

        session = await self.get_session(year, grand_prix, session_type)
        data = await _run_fastf1(_laps_sync, session, driver)
        response = {
            "data": data,
            "meta": {
                "source": "fastf1",
                "cached": False,
                "count": len(data),
                "cache_ttl_seconds": _telemetry_cache_ttl_seconds(),
            },
        }
        self.cache.set(key, response, ttl=_telemetry_cache_ttl_seconds())
        return response

    async def get_lap_telemetry(
        self,
        year: int,
        grand_prix: str,
        session_type: str,
        driver: str,
        lap_number: int,
        sample_rate: str = "medium",
    ) -> dict[str, Any]:
        sample_rate = normalize_sample_rate(sample_rate)
        key = _cache_key(
            "lap",
            year,
            grand_prix,
            session_type,
            driver.upper(),
            lap_number,
            sample_rate,
        )
        cached = self.cache.get(key)
        if cached is not None:
            return _with_cached_meta(cached)

        session = await self.get_session(year, grand_prix, session_type)
        response = await _run_fastf1(
            _lap_telemetry_sync,
            session,
            year,
            grand_prix,
            session_type,
            driver,
            lap_number,
            sample_rate,
        )
        self.cache.set(key, response, ttl=_telemetry_cache_ttl_seconds())
        return response

    async def compare_lap_telemetry(
        self,
        year: int,
        grand_prix: str,
        session_type: str,
        driver_a: str,
        lap_a: int,
        driver_b: str,
        lap_b: int,
        sample_rate: str = "medium",
    ) -> dict[str, Any]:
        sample_rate = normalize_sample_rate(sample_rate)
        key = _cache_key(
            "compare",
            year,
            grand_prix,
            session_type,
            driver_a.upper(),
            lap_a,
            driver_b.upper(),
            lap_b,
            sample_rate,
        )
        cached = self.cache.get(key)
        if cached is not None:
            return _with_cached_meta(cached)

        session = await self.get_session(year, grand_prix, session_type)
        response = await _run_fastf1(
            _compare_lap_telemetry_sync,
            session,
            year,
            grand_prix,
            session_type,
            driver_a,
            lap_a,
            driver_b,
            lap_b,
            sample_rate,
        )
        self.cache.set(key, response, ttl=_telemetry_cache_ttl_seconds())
        return response


_client: FastF1Client | None = None


def get_client() -> FastF1Client:
    global _client
    if _client is None:
        _client = FastF1Client()
    return _client


def normalize_sample_rate(sample_rate: str) -> str:
    if sample_rate not in SAMPLE_RATE_LIMITS:
        raise ValueError(f"sample_rate must be one of {', '.join(SAMPLE_RATE_LIMITS)}")
    return sample_rate


def downsample_telemetry(points: list[dict[str, Any]], sample_rate: str) -> list[dict[str, Any]]:
    sample_rate = normalize_sample_rate(sample_rate)
    limit = SAMPLE_RATE_LIMITS[sample_rate]
    if len(points) <= limit:
        return points
    stride = math.ceil(len(points) / limit)
    sampled = points[::stride]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def normalize_telemetry_rows(rows: Any, sample_rate: str = "medium") -> list[dict[str, Any]]:
    raw_rows = list(_iter_rows(rows))
    if not raw_rows:
        raise FastF1TelemetryUnavailableError("FastF1 telemetry is unavailable for this lap.")

    points: list[dict[str, Any]] = []
    first_distance: float | None = None
    first_time: float | None = None
    prev_time: float | None = None
    prev_speed: float | None = None
    derived_distance = 0.0

    for idx, row in enumerate(raw_rows):
        row_time = _seconds(_field(row, "Time"))
        if row_time is None:
            row_time = float(idx)
        if first_time is None:
            first_time = row_time

        distance = _f(_field(row, "Distance"))
        if distance is None:
            if prev_time is not None and prev_speed is not None:
                dt = max(0.0, row_time - prev_time)
                derived_distance += prev_speed * (1000.0 / 3600.0) * dt
            distance = derived_distance
        if first_distance is None:
            first_distance = distance

        speed = _f(_field(row, "Speed"))
        prev_time = row_time
        prev_speed = speed

        points.append({
            "distance": round(float(distance - first_distance), 3),
            "speed": _rounded_number(speed, ndigits=1),
            "throttle": _rounded_number(_f(_field(row, "Throttle")), ndigits=1),
            "brake": _boolish(_field(row, "Brake")),
            "gear": _i(_field(row, "nGear")) or _i(_field(row, "Gear")),
            "time": round(float(row_time - first_time), 3),
        })

    points = [point for point in points if point["distance"] >= 0]
    points.sort(key=lambda point: point["distance"])
    return downsample_telemetry(points, sample_rate)


def align_telemetry_delta(
    telemetry_a: list[dict[str, Any]],
    telemetry_b: list[dict[str, Any]],
    sample_rate: str = "medium",
) -> list[dict[str, float]]:
    if not telemetry_a or not telemetry_b:
        return []
    sample_rate = normalize_sample_rate(sample_rate)
    max_distance = min(telemetry_a[-1]["distance"], telemetry_b[-1]["distance"])
    if max_distance <= 0:
        return []

    max_points = SAMPLE_RATE_LIMITS[sample_rate]
    points = min(len(telemetry_a), len(telemetry_b), max_points)
    if points < 2:
        return []

    grid = np.linspace(0.0, max_distance, points)
    time_a = _interp_channel(telemetry_a, grid, "time")
    time_b = _interp_channel(telemetry_b, grid, "time")
    return [
        {"distance": round(float(distance), 3), "time_delta": round(float(delta), 3)}
        for distance, delta in zip(grid, time_a - time_b)
    ]


def _interp_channel(points: list[dict[str, Any]], grid: np.ndarray, channel: str) -> np.ndarray:
    xs = np.array([float(point["distance"]) for point in points], dtype=float)
    ys = np.array([float(point[channel]) for point in points], dtype=float)
    return np.interp(grid, xs, ys)


def _lap_telemetry_sync(
    session: Any,
    year: int,
    grand_prix: str,
    session_type: str,
    driver: str,
    lap_number: int,
    sample_rate: str,
) -> dict[str, Any]:
    lap = _select_lap(session, driver, lap_number)
    telemetry = _telemetry_from_lap(lap, sample_rate)
    return _lap_response(
        year,
        grand_prix,
        session_type,
        driver,
        lap,
        telemetry,
        cached=False,
        sample_rate=sample_rate,
    )


def _compare_lap_telemetry_sync(
    session: Any,
    year: int,
    grand_prix: str,
    session_type: str,
    driver_a: str,
    lap_a: int,
    driver_b: str,
    lap_b: int,
    sample_rate: str,
) -> dict[str, Any]:
    lap_a_row = _select_lap(session, driver_a, lap_a)
    lap_b_row = _select_lap(session, driver_b, lap_b)
    telemetry_a = _telemetry_from_lap(lap_a_row, sample_rate)
    telemetry_b = _telemetry_from_lap(lap_b_row, sample_rate)
    return {
        "driver_a": _lap_response(
            year,
            grand_prix,
            session_type,
            driver_a,
            lap_a_row,
            telemetry_a,
            cached=False,
            sample_rate=sample_rate,
            include_meta=False,
        ),
        "driver_b": _lap_response(
            year,
            grand_prix,
            session_type,
            driver_b,
            lap_b_row,
            telemetry_b,
            cached=False,
            sample_rate=sample_rate,
            include_meta=False,
        ),
        "delta": align_telemetry_delta(telemetry_a, telemetry_b, sample_rate),
        "sector_summary": sector_summary(lap_a_row, lap_b_row),
        "meta": {
            "source": "fastf1",
            "cached": False,
            "cache_ttl_seconds": _telemetry_cache_ttl_seconds(),
            "sample_rate": sample_rate,
        },
    }


def _lap_response(
    year: int,
    grand_prix: str,
    session_type: str,
    driver: str,
    lap: Any,
    telemetry: list[dict[str, Any]],
    *,
    cached: bool,
    sample_rate: str,
    include_meta: bool = True,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "driver": driver.upper(),
        "lap_number": _i(_field(lap, "LapNumber")),
        "lap_time": _seconds(_field(lap, "LapTime")),
        "session": {
            "year": year,
            "grand_prix": grand_prix,
            "type": session_type,
        },
        "telemetry": telemetry,
    }
    if include_meta:
        response["meta"] = {
            "source": "fastf1",
            "cached": cached,
            "points": len(telemetry),
            "cache_ttl_seconds": _telemetry_cache_ttl_seconds(),
            "sample_rate": sample_rate,
        }
    return response


def _telemetry_from_lap(lap: Any, sample_rate: str) -> list[dict[str, Any]]:
    try:
        telemetry = lap.get_car_data()
        if hasattr(telemetry, "add_distance"):
            telemetry = telemetry.add_distance()
    except Exception as exc:
        raise FastF1TelemetryUnavailableError(
            "FastF1 telemetry is unavailable for this lap."
        ) from exc

    return normalize_telemetry_rows(telemetry, sample_rate)


def _select_lap(session: Any, driver: str, lap_number: int) -> Any:
    if lap_number is None:
        raise FastF1LapNotFoundError("lap number is required.")

    laps = getattr(session, "laps", None)
    if laps is None:
        raise FastF1TelemetryUnavailableError("FastF1 session has no lap data.")

    driver_laps = _filter_driver(laps, driver)
    lap = _filter_lap_number(driver_laps, lap_number)
    if lap is None:
        raise FastF1LapNotFoundError(
            f"Lap {lap_number} was not found for driver {driver.upper()}."
        )
    return lap


def _filter_driver(laps: Any, driver: str) -> Any:
    if hasattr(laps, "pick_drivers"):
        selected = laps.pick_drivers(driver.upper())
        if _row_count(selected) == 0:
            raise FastF1DriverNotFoundError(f"Driver {driver.upper()} was not found.")
        return selected

    rows = [
        row for row in _iter_rows(laps)
        if str(_field(row, "Driver") or "").upper() == driver.upper()
        or str(_field(row, "DriverNumber") or "").upper() == driver.upper()
    ]
    if not rows:
        raise FastF1DriverNotFoundError(f"Driver {driver.upper()} was not found.")
    return rows


def _filter_lap_number(laps: Any, lap_number: int) -> Any | None:
    if hasattr(laps, "pick_laps"):
        selected = laps.pick_laps(lap_number)
        if _row_count(selected) == 0:
            return None
        return _first_row(selected)

    for row in _iter_rows(laps):
        if _i(_field(row, "LapNumber")) == int(lap_number):
            return row
    return None


def _laps_sync(session: Any, driver: str | None = None) -> list[dict[str, Any]]:
    laps = getattr(session, "laps", None)
    if laps is None:
        return []
    if driver:
        laps = _filter_driver(laps, driver)

    out: list[dict[str, Any]] = []
    for row in _iter_rows(laps):
        out.append({
            "driver": _field(row, "Driver"),
            "driver_number": _field(row, "DriverNumber"),
            "lap_number": _i(_field(row, "LapNumber")),
            "lap_time": _seconds(_field(row, "LapTime")),
            "sector_1_time": _seconds(_field(row, "Sector1Time")),
            "sector_2_time": _seconds(_field(row, "Sector2Time")),
            "sector_3_time": _seconds(_field(row, "Sector3Time")),
            "compound": _field(row, "Compound"),
            "tyre_life": _i(_field(row, "TyreLife")),
        })
    return out


def sector_summary(lap_a: Any, lap_b: Any) -> list[dict[str, Any]]:
    sectors: list[dict[str, Any]] = []
    for sector in (1, 2, 3):
        a_time = _seconds(_field(lap_a, f"Sector{sector}Time"))
        b_time = _seconds(_field(lap_b, f"Sector{sector}Time"))
        if a_time is None or b_time is None:
            continue
        sectors.append({
            "sector": sector,
            "driver_a_time": round(a_time, 3),
            "driver_b_time": round(b_time, 3),
            "delta": round(a_time - b_time, 3),
        })
    return sectors


def _cache_key(prefix: str, *parts: Any) -> str:
    joined = ":".join(str(part).strip().lower() for part in parts)
    return f"fastf1:{prefix}:{joined}"


def _with_cached_meta(response: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(response)
    if "meta" in copied:
        copied["meta"]["cached"] = True
    return copied


def _iter_rows(rows: Any) -> Iterable[Any]:
    if rows is None:
        return []
    if isinstance(rows, dict):
        return [rows]
    if isinstance(rows, list | tuple):
        return rows
    if hasattr(rows, "iterrows"):
        return [row for _, row in rows.iterrows()]
    try:
        return list(rows)
    except TypeError:
        return [rows]


def _first_row(rows: Any) -> Any | None:
    if hasattr(rows, "iloc"):
        return rows.iloc[0]
    items = list(_iter_rows(rows))
    return items[0] if items else None


def _row_count(rows: Any) -> int:
    try:
        return len(rows)
    except TypeError:
        return len(list(_iter_rows(rows)))


def _field(row: Any, name: str) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(name)
    if hasattr(row, "get"):
        try:
            return row.get(name)
        except Exception:
            pass
    try:
        return row[name]
    except Exception:
        return getattr(row, name, None)


def _seconds(value: Any) -> float | None:
    if value is None or _is_nat(value):
        return None
    if hasattr(value, "total_seconds"):
        return round(float(value.total_seconds()), 6)
    if isinstance(value, np.timedelta64):
        return round(float(value / np.timedelta64(1, "s")), 6)
    if isinstance(value, timedelta):
        return round(float(value.total_seconds()), 6)
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


# ---- legacy helpers used by the existing analysis path ----
def _fastest_lap_trace_sync(year: int, gp: str, session: str, driver: str) -> TelemetryTrace:
    ses = _load_session_sync(year, gp, session)
    laps = _filter_driver(ses.laps, driver)
    if hasattr(laps, "pick_fastest"):
        lap = laps.pick_fastest()
    else:
        rows = list(_iter_rows(laps))
        lap = min(rows, key=lambda row: _seconds(_field(row, "LapTime")) or float("inf"))
    tel = _telemetry_from_lap(lap, "high")

    samples = [
        TelemetrySample(
            distance=float(point["distance"]),
            speed=_f(point["speed"]),
            throttle=_f(point["throttle"]),
            brake=100.0 if point["brake"] else 0.0,
            n_gear=_i(point["gear"]),
        )
        for point in tel
    ]

    return TelemetryTrace(
        session_key=int(getattr(ses, "session_key", 0) or 0),
        driver_number=_i(_field(lap, "DriverNumber")) or 0,
        lap_number=_i(_field(lap, "LapNumber")),
        lap_time=_seconds(_field(lap, "LapTime")),
        samples=samples,
    )


async def fastest_lap_trace(year: int, gp: str, session: str, driver: str) -> TelemetryTrace:
    return await _run_fastf1(_fastest_lap_trace_sync, year, gp, session, driver)


def _corners_sync(year: int, gp: str, session: str) -> list[dict]:
    ses = _load_session_sync(year, gp, session)
    ci = ses.get_circuit_info()
    return [
        {
            "corner_number": int(_field(row, "Number")),
            "distance": float(_field(row, "Distance")),
            "angle": float(_field(row, "Angle") or 0.0),
        }
        for row in _iter_rows(ci.corners)
    ]


async def circuit_corners(year: int, gp: str, session: str) -> list[dict]:
    return await _run_fastf1(_corners_sync, year, gp, session)


# ---- coercion helpers ----
def _rounded_number(value: float | None, *, ndigits: int) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _boolish(value: Any) -> bool:
    if value is None or _is_nat(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _is_nat(v: Any) -> bool:
    return str(v) in {"NaT", "nan", "NaN"}
