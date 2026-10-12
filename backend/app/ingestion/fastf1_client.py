"""Thin FastF1 adapter — the ONLY module in the backend that imports `fastf1`.

It returns plain pandas DataFrames and dicts and translates FastF1 failures into
SessionNotFoundError / TelemetryUnavailableError, so nothing else ever touches a
FastF1 object (tests/test_ingestion_isolation.py enforces this). `fastf1` is imported
lazily so importing this module stays cheap.

Verified against FastF1 3.5.3 — see docs/decisions.md for the probed schemas.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .errors import SessionNotFoundError, TelemetryUnavailableError
from .normalize import normalize_session_code

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache_dir: str | None = None


def import_fastf1(cache_dir: str | Path | None = None) -> Any:
    """Import FastF1 and enable its on-disk cache once per process.

    The legacy chart client (app/clients/fastf1_client.py) also goes through here so
    there is exactly one import site.
    """
    global _cache_dir
    import fastf1

    with _lock:
        if cache_dir is not None and _cache_dir is None:
            path = Path(cache_dir)
            path.mkdir(parents=True, exist_ok=True)
            fastf1.Cache.enable_cache(str(path))
            fastf1.set_log_level("WARNING")
            logging.getLogger("fastf1").setLevel(logging.WARNING)
            _cache_dir = str(path)
    return fastf1


def fastf1_version() -> str:
    try:
        from importlib.metadata import version

        return version("fastf1")
    except Exception:  # pragma: no cover - only when the package is absent
        return "unknown"


@dataclass
class RawSession:
    """Raw frames for one loaded session. `_laps_handle` is the opaque FastF1 Laps
    object kept only so `FastF1Client.lap_telemetry` can reach per-lap telemetry."""

    year: int
    round: int
    session_code: str
    info: dict[str, Any]
    laps: pd.DataFrame
    results: pd.DataFrame
    weather: pd.DataFrame
    corners: pd.DataFrame
    fastf1_version: str
    _laps_handle: Any = field(default=None, repr=False)
    _lap_index: dict[tuple[str, int], int] = field(default_factory=dict, repr=False)


class FastF1Client:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)

    @property
    def fastf1_version(self) -> str:
        return fastf1_version()

    def load_session(self, year: int, round_number: int, session_code: str) -> RawSession:
        code = normalize_session_code(session_code)
        fastf1 = import_fastf1(self.cache_dir)
        label = f"{year} round {round_number} {code}"
        try:
            session = fastf1.get_session(year, round_number, code)
            # messages=True is required: FastF1 only fills `Deleted` from race control.
            session.load(laps=True, telemetry=True, weather=True, messages=True)
            laps = session.laps
        except Exception as exc:
            raise SessionNotFoundError(f"FastF1 could not load {label}: {exc}") from exc
        if laps is None or len(laps) == 0:
            raise SessionNotFoundError(f"FastF1 returned no laps for {label}.")

        event = session.event
        corners, rotation = _circuit(session, label)
        info = {
            "year": int(year),
            "round": int(event.get("RoundNumber", round_number)),
            "event_name": event.get("EventName"),
            "location": event.get("Location"),
            "country": event.get("Country"),
            "event_format": event.get("EventFormat"),
            "session_name": session.name,
            "session_code": code,
            "date": session.date,
            "total_laps": _safe_attr(session, "total_laps"),
            "circuit_rotation": rotation,
        }
        lap_index: dict[tuple[str, int], int] = {}
        for pos, (driver, lap_number) in enumerate(zip(laps["Driver"], laps["LapNumber"])):
            if isinstance(driver, str) and pd.notna(lap_number):
                lap_index.setdefault((driver, int(lap_number)), pos)

        return RawSession(
            year=int(year),
            round=int(round_number),
            session_code=code,
            info=info,
            laps=pd.DataFrame(laps),
            results=_frame(lambda: session.results, "results", label),
            weather=_frame(lambda: session.weather_data, "weather", label),
            corners=corners,
            fastf1_version=self.fastf1_version,
            _laps_handle=laps,
            _lap_index=lap_index,
        )

    def lap_telemetry(self, raw: RawSession, driver: str, lap_number: int) -> pd.DataFrame:
        """Merged car + position telemetry with Distance (`Lap.get_telemetry()`)."""
        pos = raw._lap_index.get((driver, int(lap_number)))
        if pos is None or raw._laps_handle is None:
            raise TelemetryUnavailableError(f"No lap {lap_number} for {driver}.")
        try:
            tel = raw._laps_handle.iloc[pos].get_telemetry()
            if "Distance" not in tel.columns:
                tel = tel.add_distance()
        except Exception as exc:
            raise TelemetryUnavailableError(
                f"Telemetry unavailable for {driver} lap {lap_number}: {exc}"
            ) from exc
        if tel is None or len(tel) == 0:
            raise TelemetryUnavailableError(f"Empty telemetry for {driver} lap {lap_number}.")
        return pd.DataFrame(tel)

    def event_schedule(self, year: int) -> pd.DataFrame:
        fastf1 = import_fastf1(self.cache_dir)
        try:
            return pd.DataFrame(fastf1.get_event_schedule(year, include_testing=False))
        except Exception as exc:
            raise SessionNotFoundError(f"FastF1 has no schedule for {year}: {exc}") from exc


def _safe_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _frame(getter: Any, what: str, label: str) -> pd.DataFrame:
    try:
        value = getter()
    except Exception as exc:
        logger.warning("FastF1 %s unavailable for %s: %s", what, label, exc)
        return pd.DataFrame()
    return pd.DataFrame(value) if value is not None else pd.DataFrame()


def _circuit(session: Any, label: str) -> tuple[pd.DataFrame, float | None]:
    try:
        info = session.get_circuit_info()
    except Exception as exc:
        logger.warning("FastF1 circuit info unavailable for %s: %s", label, exc)
        return pd.DataFrame(), None
    if info is None:
        return pd.DataFrame(), None
    corners = getattr(info, "corners", None)
    rotation = getattr(info, "rotation", None)
    return (
        pd.DataFrame(corners) if corners is not None else pd.DataFrame(),
        float(rotation) if rotation is not None else None,
    )
