"""Pure functions: FastF1-shaped pandas frames in, domain models out.

No I/O and no FastF1 import — everything here is testable with synthetic frames.
Timedeltas become float seconds at this boundary; NaT/NaN become None.
"""
from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .models import (
    CornerInfo,
    DriverInfo,
    EventInfo,
    EventSession,
    LapKey,
    FloatArray,
    LapRecord,
    SessionInfo,
    TelemetryFrame,
    WeatherSample,
)

# DRS codes that mean "flap open". Verified against FastF1 3.5.3 (`fastf1/_api.py`:
# "DRS (int): 0-14 (Odd DRS is Disabled, Even DRS is Enabled?)") plus the codes
# observed in 2024 R5 car data: 0,1,2,3,8,9,10,11,12,14. 8 = eligible/armed but
# closed; even codes >= 10 = open; odd codes are disabled. See docs/decisions.md.
DRS_OPEN_CODES: frozenset[int] = frozenset({10, 12, 14})

MIN_TELEMETRY_SAMPLES = 200
DISTANCE_TOLERANCE = 0.03  # ±3 % of the session's median lap distance
MS_TO_KPH = 3.6

SESSION_CODES: tuple[str, ...] = ("FP1", "FP2", "FP3", "SQ", "SS", "S", "Q", "R")
SESSION_NAME_TO_CODE: dict[str, str] = {
    "practice 1": "FP1",
    "practice 2": "FP2",
    "practice 3": "FP3",
    "sprint qualifying": "SQ",
    "sprint shootout": "SS",
    "sprint": "S",
    "qualifying": "Q",
    "race": "R",
}

TelemetryStats = Mapping[LapKey, tuple[int, float | None]]  # (samples, lap distance m)


# ---------------------------------------------------------------- scalars
def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NaT:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def to_seconds(value: Any) -> float | None:
    """Timedelta-like or number → float seconds. NaT/NaN/None/garbage → None."""
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timedelta | timedelta):
        return float(value.total_seconds())
    if isinstance(value, np.timedelta64):
        return float(value / np.timedelta64(1, "ns")) / 1e9
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | np.integer | np.floating):
        return float(value)
    return None


def _float(value: Any) -> float | None:
    if _is_missing(value) or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _int(value: Any) -> int | None:
    out = _float(value)
    return None if out is None else int(out)


def _bool(value: Any) -> bool | None:
    if _is_missing(value):
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
        return None
    return bool(value)


def _str(value: Any) -> str | None:
    if _is_missing(value):
        return None
    out = str(value).strip()
    return out or None


def _utc(value: Any) -> datetime | None:
    """FastF1 dates are naive but already UTC; attach the zone instead of shifting."""
    if _is_missing(value):
        return None
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize(timezone.utc) if stamp.tzinfo is None else stamp.tz_convert(timezone.utc)
    out: datetime = stamp.to_pydatetime()
    return out


def _col(df: pd.DataFrame, name: str) -> list[Any]:
    if name in df.columns:
        return list(df[name])
    return [None] * len(df)


def raw_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → JSON-safe list of dicts (numpy scalars unwrapped, NaN → None)."""
    out: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if _is_missing(value):
                clean[str(key)] = None
            elif isinstance(value, pd.Timedelta | timedelta | np.timedelta64):
                clean[str(key)] = to_seconds(value)
            elif isinstance(value, np.generic):
                clean[str(key)] = value.item()
            else:
                clean[str(key)] = value
        out.append(clean)
    return out


# ---------------------------------------------------------------- keys / codes
def normalize_session_code(code: str) -> str:
    """Accept FastF1 identifiers ("q", "FP1") or session names ("Qualifying")."""
    cleaned = code.strip()
    upper = cleaned.upper()
    if upper in SESSION_CODES:
        return upper
    mapped = SESSION_NAME_TO_CODE.get(cleaned.lower())
    if mapped is None:
        raise ValueError(f"unknown session {code!r}; expected one of {', '.join(SESSION_CODES)}")
    return mapped


def make_session_key(year: int, round_number: int, session_code: str) -> str:
    return f"{int(year)}_{int(round_number):02d}_{normalize_session_code(session_code)}"


# ---------------------------------------------------------------- DRS
def normalize_drs(code: Any) -> bool:
    value = _int(code)
    return value is not None and value in DRS_OPEN_CODES


# ---------------------------------------------------------------- validity
def lap_invalid_reason(
    *,
    lap_time_s: float | None,
    is_accurate: bool,
    deleted: bool | None,
    n_samples: int,
    distance_m: float | None,
    median_distance_m: float | None,
) -> str | None:
    """First failing rule, or None when the lap is fit for lap-vs-lap comparison."""
    if lap_time_s is None:
        return "no_lap_time"
    if not is_accurate:
        return "not_accurate"
    if deleted is True:
        return "deleted"
    if n_samples <= 0 or distance_m is None:
        return "no_telemetry"
    if n_samples < MIN_TELEMETRY_SAMPLES:
        return "too_few_samples"
    if median_distance_m is None or median_distance_m <= 0:
        return "distance_out_of_range"
    if abs(distance_m - median_distance_m) > DISTANCE_TOLERANCE * median_distance_m:
        return "distance_out_of_range"
    return None


# ---------------------------------------------------------------- weather
def normalize_weather(df: pd.DataFrame) -> list[WeatherSample]:
    samples: list[WeatherSample] = []
    for t, air, track, hum, rain, wind in zip(
        _col(df, "Time"), _col(df, "AirTemp"), _col(df, "TrackTemp"),
        _col(df, "Humidity"), _col(df, "Rainfall"), _col(df, "WindSpeed"),
    ):
        time_s = to_seconds(t)
        if time_s is None:
            continue
        wind_ms = _float(wind)
        samples.append(WeatherSample(
            time_s=time_s,
            air_temp_c=_float(air),
            track_temp_c=_float(track),
            humidity=_float(hum),
            rainfall=_bool(rain),
            wind_kph=None if wind_ms is None else round(wind_ms * MS_TO_KPH, 3),
        ))
    samples.sort(key=lambda sample: sample.time_s)
    return samples


def nearest_weather(samples: Sequence[WeatherSample], time_s: float | None) -> WeatherSample | None:
    """Sample closest in session time (ties go to the earlier one). `samples` sorted."""
    if time_s is None or not samples:
        return None
    times = [sample.time_s for sample in samples]
    idx = bisect.bisect_left(times, time_s)
    if idx == 0:
        return samples[0]
    if idx == len(samples):
        return samples[-1]
    before, after = samples[idx - 1], samples[idx]
    return before if time_s - before.time_s <= after.time_s - time_s else after


# ---------------------------------------------------------------- laps
def normalize_laps(
    df: pd.DataFrame,
    session_key: str,
    *,
    weather: Sequence[WeatherSample] = (),
    telemetry_stats: TelemetryStats | None = None,
) -> list[LapRecord]:
    """FastF1 `session.laps` → LapRecords with weather and validity attached.

    Rows without a driver or lap number cannot be addressed and are dropped.
    `telemetry_stats` maps (driver, lap) to (sample count, lap distance); laps missing
    from it have no telemetry.
    """
    if df.empty:
        return []
    stats: TelemetryStats = telemetry_stats or {}
    weather = sorted(weather, key=lambda sample: sample.time_s)

    base: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        driver = _str(row.get("Driver"))
        lap_number = _int(row.get("LapNumber"))
        if driver is None or lap_number is None:
            continue
        n_samples, distance = stats.get((driver, lap_number), (0, None))
        lap_start = to_seconds(row.get("LapStartTime"))
        sample = nearest_weather(weather, lap_start)
        base.append({
            "session_key": session_key,
            "driver": driver,
            "driver_number": _str(row.get("DriverNumber")) or "",
            "team": _str(row.get("Team")),
            "lap_number": lap_number,
            "lap_time_s": to_seconds(row.get("LapTime")),
            "sector_s": tuple(to_seconds(row.get(f"Sector{i}Time")) for i in (1, 2, 3)),
            "stint": _int(row.get("Stint")),
            "compound": _str(row.get("Compound")),
            "tyre_age": _int(row.get("TyreLife")),
            "fresh_tyre": _bool(row.get("FreshTyre")),
            "pit_in_time_s": to_seconds(row.get("PitInTime")),
            "pit_out_time_s": to_seconds(row.get("PitOutTime")),
            "track_status": _str(row.get("TrackStatus")),
            "is_accurate": _bool(row.get("IsAccurate")) is True,
            "deleted": _bool(row.get("Deleted")),
            "deleted_reason": _str(row.get("DeletedReason")),
            "position": _int(row.get("Position")),
            "lap_start_session_time_s": lap_start,
            "lap_end_session_time_s": to_seconds(row.get("Time")),
            "air_temp_c": sample.air_temp_c if sample else None,
            "track_temp_c": sample.track_temp_c if sample else None,
            "rainfall": sample.rainfall if sample else None,
            "telemetry_samples": int(n_samples),
            "telemetry_distance_m": distance,
        })

    distances = [
        rec["telemetry_distance_m"] for rec in base
        if rec["lap_time_s"] is not None and rec["telemetry_distance_m"] is not None
    ]
    median = statistics.median(distances) if distances else None

    records: list[LapRecord] = []
    for rec in base:
        reason = lap_invalid_reason(
            lap_time_s=rec["lap_time_s"],
            is_accurate=rec["is_accurate"],
            deleted=rec["deleted"],
            n_samples=rec["telemetry_samples"],
            distance_m=rec["telemetry_distance_m"],
            median_distance_m=median,
        )
        records.append(LapRecord(**rec, is_valid_for_comparison=reason is None, invalid_reason=reason))
    return records


# ---------------------------------------------------------------- telemetry
def _float_array(df: pd.DataFrame, name: str) -> FloatArray:
    if name not in df.columns:
        return np.full(len(df), np.nan, dtype=np.float64)
    return pd.to_numeric(df[name], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan)


def _time_array(df: pd.DataFrame, name: str) -> FloatArray:
    if name not in df.columns:
        return np.full(len(df), np.nan, dtype=np.float64)
    col = df[name]
    if pd.api.types.is_timedelta64_dtype(col):
        return col.dt.total_seconds().to_numpy(dtype=np.float64, na_value=np.nan)
    return np.array([to_seconds(v) if to_seconds(v) is not None else np.nan for v in col], dtype=np.float64)


def _fill_nan_by_interp(values: FloatArray, xs: FloatArray) -> FloatArray:
    ok = ~np.isnan(values)
    if ok.all() or not ok.any():
        return np.where(ok, values, 0.0)
    return np.interp(xs, xs[ok], values[ok])


def _integrate_distance(time_s: FloatArray, speed_kph: FloatArray) -> FloatArray:
    """Trapezoidal integral of speed over time, starting at 0 (same as add_distance)."""
    if len(time_s) == 0:
        return np.zeros(0, dtype=np.float64)
    speed_ms = np.clip(_fill_nan_by_interp(speed_kph, time_s), 0.0, None) / MS_TO_KPH
    steps = np.diff(time_s) * (speed_ms[1:] + speed_ms[:-1]) / 2.0
    return np.concatenate(([0.0], np.cumsum(steps)))


def normalize_telemetry(df: pd.DataFrame) -> TelemetryFrame:
    """FastF1 `lap.get_telemetry()` → TelemetryFrame.

    Rows without a time are dropped, rows are sorted by time and duplicate timestamps
    removed (time_s strictly increasing). Distance is taken from FastF1 when complete,
    otherwise integrated from speed; either way it is forced non-decreasing.
    """
    if df.empty:
        return TelemetryFrame.empty()

    time_s = _time_array(df, "Time")
    keep = ~np.isnan(time_s)
    df = df.loc[keep].copy()
    time_s = time_s[keep]
    order = np.argsort(time_s, kind="stable")
    df, time_s = df.iloc[order], time_s[order]
    unique = np.concatenate(([True], np.diff(time_s) > 0)) if len(time_s) else np.zeros(0, dtype=bool)
    df, time_s = df.loc[unique], time_s[unique]

    speed = _float_array(df, "Speed")
    distance = _float_array(df, "Distance")
    if len(distance) and np.isnan(distance).any():
        distance = _integrate_distance(time_s, speed)
    distance = np.maximum.accumulate(distance) if len(distance) else distance

    if "Brake" in df.columns and pd.api.types.is_bool_dtype(df["Brake"]):
        brake = df["Brake"].to_numpy(dtype=np.bool_)
    else:
        brake = np.nan_to_num(_float_array(df, "Brake"), nan=0.0) > 0

    drs_code = np.nan_to_num(_float_array(df, "DRS"), nan=0.0).astype(np.int64)
    return TelemetryFrame(
        time_s=time_s.astype(np.float64),
        distance_m=distance.astype(np.float64),
        speed_kph=speed,
        throttle_pct=_float_array(df, "Throttle"),
        brake=brake,
        gear=np.nan_to_num(_float_array(df, "nGear"), nan=0.0).astype(np.int64),
        rpm=_float_array(df, "RPM"),
        drs_code=drs_code,
        drs_open=np.isin(drs_code, list(DRS_OPEN_CODES)),
        x=_float_array(df, "X"),
        y=_float_array(df, "Y"),
    )


def telemetry_stats(frame: TelemetryFrame) -> tuple[int, float | None]:
    return len(frame), frame.total_distance_m


# ---------------------------------------------------------------- circuit / drivers / session
def normalize_corners(df: pd.DataFrame) -> list[CornerInfo]:
    corners: list[CornerInfo] = []
    for number, letter, distance, x, y, angle in zip(
        _col(df, "Number"), _col(df, "Letter"), _col(df, "Distance"),
        _col(df, "X"), _col(df, "Y"), _col(df, "Angle"),
    ):
        num, dist = _int(number), _float(distance)
        if num is None or dist is None:
            continue
        corners.append(CornerInfo(
            number=num,
            letter=_str(letter),
            distance_m=dist,
            x=_float(x) or 0.0,
            y=_float(y) or 0.0,
            angle=_float(angle) or 0.0,
        ))
    corners.sort(key=lambda corner: corner.distance_m)
    return corners


def _team_color(value: Any) -> str | None:
    raw = _str(value)
    if raw is None or raw.lower() == "nan":
        return None
    return raw if raw.startswith("#") else f"#{raw}"


def normalize_drivers(df: pd.DataFrame) -> list[DriverInfo]:
    drivers: list[DriverInfo] = []
    for row in df.to_dict(orient="records"):
        code = _str(row.get("Abbreviation"))
        if code is None:
            continue
        drivers.append(DriverInfo(
            code=code,
            number=_str(row.get("DriverNumber")) or "",
            full_name=_str(row.get("FullName")),
            team=_str(row.get("TeamName")),
            team_color=_team_color(row.get("TeamColor")),
            position=_int(row.get("Position")),
            grid_position=_int(row.get("GridPosition")),
            status=_str(row.get("Status")),
            q1_s=to_seconds(row.get("Q1")),
            q2_s=to_seconds(row.get("Q2")),
            q3_s=to_seconds(row.get("Q3")),
        ))
    return drivers


def normalize_session_info(info: Mapping[str, Any]) -> SessionInfo:
    """`info` is the plain dict `fastf1_client.RawSession.info` carries."""
    code = normalize_session_code(str(info["session_code"]))
    date = _utc(info.get("date"))
    if date is None:
        raise ValueError("session has no start date")
    return SessionInfo(
        key=make_session_key(int(info["year"]), int(info["round"]), code),
        year=int(info["year"]),
        round=int(info["round"]),
        event_name=str(info.get("event_name") or ""),
        circuit=str(info.get("location") or ""),
        country=str(info.get("country") or ""),
        session_code=code,
        session_name=str(info.get("session_name") or code),
        date=date,
        n_laps_scheduled=_int(info.get("total_laps")),
        event_format=_str(info.get("event_format")),
        circuit_rotation_deg=_float(info.get("circuit_rotation")),
    )


def normalize_schedule(df: pd.DataFrame, year: int) -> list[EventInfo]:
    events: list[EventInfo] = []
    for row in df.to_dict(orient="records"):
        round_number = _int(row.get("RoundNumber"))
        if not round_number:  # round 0 = pre-season testing
            continue
        sessions: list[EventSession] = []
        for slot in range(1, 6):
            name = _str(row.get(f"Session{slot}"))
            if name is None:
                continue
            sessions.append(EventSession(
                name=name,
                code=SESSION_NAME_TO_CODE.get(name.lower()),
                date=_utc(row.get(f"Session{slot}DateUtc")),
            ))
        events.append(EventInfo(
            year=year,
            round=round_number,
            event_name=str(row.get("EventName") or ""),
            country=str(row.get("Country") or ""),
            location=str(row.get("Location") or ""),
            event_format=str(row.get("EventFormat") or ""),
            event_date=_utc(row.get("EventDate")),
            sessions=tuple(sessions),
        ))
    return events
