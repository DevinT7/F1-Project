"""Session storage: the SessionRepository protocol and its Parquet implementation.

Layout under the data root:

    {key}/manifest.json                 written last; its presence marks a complete session
    {key}/session.json  drivers.json  corners_raw.json
    {key}/laps.parquet  weather.parquet  corners.parquet
    {key}/telemetry/{driver}_{lap:03d}.parquet
    _schedule/{year}.json               cached event schedule

Every file is written atomically (temp file + os.replace) and a whole session is
staged in a sibling directory and swapped in, so readers never see a half-written
session. A manifest whose schema_version differs from models.SCHEMA_VERSION makes
the session count as absent, which triggers a re-ingest.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from ..ingestion.errors import SessionNotFoundError, TelemetryUnavailableError
from ..ingestion.models import (
    SCHEMA_VERSION,
    CornerInfo,
    DriverInfo,
    EventInfo,
    LapRecord,
    Manifest,
    SessionBundle,
    SessionInfo,
    TelemetryFrame,
    WeatherSample,
)

_KEY_RE = re.compile(r"^\d{4}_\d{2}_[A-Z0-9]{1,3}$")
_DRIVER_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")


class SessionRepository(Protocol):
    def has_session(self, key: str) -> bool: ...
    def save_session(self, bundle: SessionBundle) -> None: ...
    def get_session_info(self, key: str) -> SessionInfo: ...
    def list_drivers(self, key: str) -> list[DriverInfo]: ...
    def list_laps(self, key: str, driver: str | None = None) -> list[LapRecord]: ...
    def get_telemetry(self, key: str, driver: str, lap: int) -> TelemetryFrame: ...
    def get_corners(self, key: str) -> list[CornerInfo]: ...
    def get_weather(self, key: str) -> list[WeatherSample]: ...
    def list_sessions(self, year: int | None = None) -> list[SessionInfo]: ...
    def get_manifest(self, key: str) -> Manifest | None: ...
    def save_events(self, year: int, events: list[EventInfo]) -> None: ...
    def get_events(self, year: int) -> tuple[datetime, list[EventInfo]] | None: ...


# ---------------------------------------------------------------- atomic file helpers
def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then rename over the target.

    A crash or exception mid-write leaves the previous file (if any) untouched and
    no temp file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _table_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue()


def _models_table(models: Iterable[BaseModel]) -> pa.Table:
    rows = [model.model_dump(mode="python") for model in models]
    return pa.Table.from_pylist(rows)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise ValueError(f"invalid session key {key!r}")
    return key


def telemetry_filename(driver: str, lap: int) -> str:
    if not _DRIVER_RE.match(driver):
        raise ValueError(f"invalid driver code {driver!r}")
    return f"{driver}_{int(lap):03d}.parquet"


def _replace_with_retry(src: Path, dst: Path, attempts: int = 6, delay_s: float = 0.5) -> None:
    """os.replace a directory, retrying briefly on PermissionError.

    On Windows a freshly written directory can be held open for a moment by antivirus or the
    search indexer, so the rename fails with "Access is denied" (seen while bulk-ingesting
    2023 R15). Retrying for ~3 s rides that out; a persistent lock still raises.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_s * (attempt + 1) / 2)


# ---------------------------------------------------------------- repository
class ParquetRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # -- paths
    def _dir(self, key: str) -> Path:
        return self.root / _check_key(key)

    def _require(self, key: str) -> Path:
        if not self.has_session(key):
            raise SessionNotFoundError(f"Session {key} is not stored (or its schema is stale).")
        return self._dir(key)

    # -- manifest / presence
    def get_manifest(self, key: str) -> Manifest | None:
        path = self._dir(key) / "manifest.json"
        if not path.exists():
            return None
        try:
            return Manifest.model_validate_json(path.read_bytes())
        except ValueError:
            return None

    def has_session(self, key: str) -> bool:
        manifest = self.get_manifest(key)
        return manifest is not None and manifest.schema_version == SCHEMA_VERSION

    # -- write
    def save_session(self, bundle: SessionBundle) -> None:
        key = _check_key(bundle.info.key)
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".staging-{key}-{uuid.uuid4().hex[:8]}"
        try:
            self._write_bundle(staging, bundle)
            self._swap_in(staging, self._dir(key))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _write_bundle(self, target: Path, bundle: SessionBundle) -> None:
        tel_dir = target / "telemetry"
        tel_dir.mkdir(parents=True, exist_ok=True)
        for (driver, lap), frame in bundle.telemetry.items():
            table = pa.table(frame.columns())
            atomic_write_bytes(tel_dir / telemetry_filename(driver, lap), _table_bytes(table))

        atomic_write_bytes(target / "laps.parquet", _table_bytes(_models_table(bundle.laps)))
        atomic_write_bytes(target / "weather.parquet", _table_bytes(_models_table(bundle.weather)))
        atomic_write_bytes(target / "corners.parquet", _table_bytes(_models_table(bundle.corners)))
        atomic_write_json(target / "session.json", bundle.info.model_dump(mode="json"))
        atomic_write_json(target / "drivers.json", [d.model_dump(mode="json") for d in bundle.drivers])
        atomic_write_json(target / "corners_raw.json", bundle.corners_raw)

        manifest = Manifest(
            session_key=bundle.info.key,
            ingested_at=datetime.now(timezone.utc),
            fastf1_version=bundle.fastf1_version,
            schema_version=SCHEMA_VERSION,
            lap_count=len(bundle.laps),
            telemetry_count=len(bundle.telemetry),
            driver_count=len(bundle.drivers),
        )
        atomic_write_json(target / "manifest.json", manifest.model_dump(mode="json"))

    def _swap_in(self, staging: Path, final: Path) -> None:
        # os.replace cannot overwrite a non-empty directory (Windows or POSIX), so move
        # the old copy aside first and only delete it once the new one is in place.
        trash: Path | None = None
        if final.exists():
            trash = final.with_name(f".trash-{final.name}-{uuid.uuid4().hex[:8]}")
            _replace_with_retry(final, trash)
        try:
            _replace_with_retry(staging, final)
        except BaseException:
            if trash is not None:
                _replace_with_retry(trash, final)
            raise
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)

    # -- read
    def get_session_info(self, key: str) -> SessionInfo:
        return SessionInfo.model_validate_json((self._require(key) / "session.json").read_bytes())

    def list_drivers(self, key: str) -> list[DriverInfo]:
        raw = json.loads((self._require(key) / "drivers.json").read_text("utf-8"))
        return [DriverInfo.model_validate(item) for item in raw]

    def list_laps(self, key: str, driver: str | None = None) -> list[LapRecord]:
        laps = [LapRecord.model_validate(row) for row in _read_rows(self._require(key) / "laps.parquet")]
        if driver is not None:
            wanted = driver.upper()
            laps = [lap for lap in laps if lap.driver.upper() == wanted or lap.driver_number == driver]
        return laps

    def get_telemetry(self, key: str, driver: str, lap: int) -> TelemetryFrame:
        path = self._require(key) / "telemetry" / telemetry_filename(driver, lap)
        if not path.exists():
            raise TelemetryUnavailableError(f"No stored telemetry for {driver} lap {lap} in {key}.")
        table = pq.read_table(path)
        return TelemetryFrame.from_columns({name: table.column(name).to_numpy() for name in table.column_names})

    def get_corners(self, key: str) -> list[CornerInfo]:
        return [CornerInfo.model_validate(row) for row in _read_rows(self._require(key) / "corners.parquet")]

    def get_weather(self, key: str) -> list[WeatherSample]:
        return [WeatherSample.model_validate(row) for row in _read_rows(self._require(key) / "weather.parquet")]

    def get_corners_raw(self, key: str) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = json.loads((self._require(key) / "corners_raw.json").read_text("utf-8"))
        return raw

    def list_sessions(self, year: int | None = None) -> list[SessionInfo]:
        if not self.root.exists():
            return []
        sessions: list[SessionInfo] = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or not _KEY_RE.match(child.name) or not self.has_session(child.name):
                continue
            info = self.get_session_info(child.name)
            if year is None or info.year == year:
                sessions.append(info)
        return sessions

    # -- schedule cache
    def _events_path(self, year: int) -> Path:
        return self.root / "_schedule" / f"{int(year)}.json"

    def save_events(self, year: int, events: list[EventInfo]) -> None:
        atomic_write_json(self._events_path(year), {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "events": [event.model_dump(mode="json") for event in events],
        })

    def get_events(self, year: int) -> tuple[datetime, list[EventInfo]] | None:
        path = self._events_path(year)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                return None
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            return fetched_at, [EventInfo.model_validate(item) for item in payload["events"]]
        except (ValueError, KeyError, TypeError):
            return None
