"""Phase 8: SessionRepository on PostgreSQL (+ TimescaleDB for telemetry).

Same contract as ParquetRepository (tests/test_repository_contract.py runs against both):

  * save_session replaces a session in ONE transaction (delete + insert), so readers never see a
    half-written session and re-ingesting is idempotent (unique keys guard against duplicates)
  * telemetry is bulk-loaded with COPY (row-by-row INSERT is far too slow for ~700 samples × laps)
  * a session whose stored schema_version differs from SCHEMA_VERSION counts as absent
  * session keys and driver codes are validated exactly like the Parquet store (never interpolated
    into SQL; every value is a bound parameter or a COPY row)

telemetry_samples.ts (the hypertable's time column) is the absolute sample time: session start +
lap start (session time) + lap-relative time; laps without a start time fall back to a
deterministic slot so the column is never null.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from sqlalchemy import Connection, Engine, delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db.schema import corners, drivers, event_schedules, laps, sessions, telemetry_samples, weather
from ..ingestion.errors import SessionNotFoundError, TelemetryUnavailableError
from ..ingestion.models import (
    SCHEMA_VERSION,
    TELEMETRY_COLUMNS,
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
from .repository import _DRIVER_RE, _check_key

__all__ = ["PostgresRepository", "SCHEMA_VERSION"]

_COPY_COLUMNS = ("ts", "lap_id", "sample_idx", *TELEMETRY_COLUMNS)
_FALLBACK_LAP_SLOT_S = 300.0


def _check_driver(driver: str) -> str:
    if not _DRIVER_RE.match(driver):
        raise ValueError(f"invalid driver code {driver!r}")
    return driver


def _json(model: Any) -> Any:
    return model.model_dump(mode="json")


class PostgresRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------ presence
    def get_manifest(self, key: str) -> Manifest | None:
        _check_key(key)
        with self.engine.connect() as conn:
            row = conn.execute(select(sessions).where(sessions.c.key == key)).mappings().first()
        if row is None:
            return None
        return Manifest(session_key=row["key"], ingested_at=row["ingested_at"], fastf1_version=row["fastf1_version"],
                        schema_version=row["schema_version"], lap_count=row["lap_count"],
                        telemetry_count=row["telemetry_count"], driver_count=row["driver_count"])

    def has_session(self, key: str) -> bool:
        manifest = self.get_manifest(key)
        return manifest is not None and manifest.schema_version == SCHEMA_VERSION

    def _require(self, conn: Connection, key: str) -> int:
        _check_key(key)
        sid = conn.execute(select(sessions.c.id).where(sessions.c.key == key,
                                                       sessions.c.schema_version == SCHEMA_VERSION)).scalar()
        if sid is None:
            raise SessionNotFoundError(f"Session {key} is not stored (or its schema is stale).")
        return int(sid)

    # ------------------------------------------------------------ write
    def save_session(self, bundle: SessionBundle) -> None:
        key = _check_key(bundle.info.key)
        lap_keys = {(lap.driver, lap.lap_number) for lap in bundle.laps}
        for driver, lap_number in bundle.telemetry:          # validate everything before touching the db
            _check_driver(driver)
            if (driver, lap_number) not in lap_keys:
                raise ValueError(f"telemetry for {driver} lap {lap_number} has no lap record")
        for lap in bundle.laps:
            _check_driver(lap.driver)
        with self.engine.begin() as conn:                     # one transaction: all or nothing
            old = conn.execute(select(sessions.c.id).where(sessions.c.key == key)).scalar()
            if old is not None:
                conn.execute(delete(telemetry_samples).where(
                    telemetry_samples.c.lap_id.in_(select(laps.c.id).where(laps.c.session_id == old))))
                conn.execute(delete(sessions).where(sessions.c.id == old))        # cascades to the children
            sid = conn.execute(insert(sessions).returning(sessions.c.id), {
                "key": key, "year": bundle.info.year, "round": bundle.info.round,
                "session_code": bundle.info.session_code, "info": _json(bundle.info), "corners_raw": bundle.corners_raw,
                "ingested_at": datetime.now(timezone.utc), "fastf1_version": bundle.fastf1_version,
                "schema_version": SCHEMA_VERSION, "lap_count": len(bundle.laps),
                "telemetry_count": len(bundle.telemetry), "driver_count": len(bundle.drivers),
            }).scalar_one()
            if bundle.drivers:
                conn.execute(insert(drivers), [{"session_id": sid, "ord": i, "code": d.code, "number": d.number,
                                                "team": d.team, "data": _json(d)} for i, d in enumerate(bundle.drivers)])
            lap_ids: dict[tuple[str, int], int] = {}
            if bundle.laps:
                rows = conn.execute(insert(laps).returning(laps.c.id, laps.c.driver, laps.c.lap_number,
                                                           sort_by_parameter_order=True), [{
                    "session_id": sid, "session_key": key, "ord": i, "driver": lap.driver,
                    "driver_number": lap.driver_number, "lap_number": lap.lap_number, "lap_time_s": lap.lap_time_s,
                    "compound": lap.compound, "stint": lap.stint,
                    "is_valid_for_comparison": lap.is_valid_for_comparison,
                    "telemetry_stored": (lap.driver, lap.lap_number) in bundle.telemetry, "data": _json(lap),
                } for i, lap in enumerate(bundle.laps)]).all()
                lap_ids = {(r.driver, r.lap_number): int(r.id) for r in rows}
            if bundle.corners:
                conn.execute(insert(corners), [{"session_id": sid, "ord": i, **c.model_dump()}
                                               for i, c in enumerate(bundle.corners)])
            if bundle.weather:
                conn.execute(insert(weather), [{"session_id": sid, "ord": i, **w.model_dump()}
                                               for i, w in enumerate(bundle.weather)])
            starts = {(lap.driver, lap.lap_number): lap.lap_start_session_time_s for lap in bundle.laps}
            self._copy_telemetry(conn, bundle, lap_ids, starts)

    def _copy_telemetry(self, conn: Connection, bundle: SessionBundle, lap_ids: dict[tuple[str, int], int],
                        starts: dict[tuple[str, int], float | None]) -> None:
        if not bundle.telemetry:
            return
        raw = conn.connection.dbapi_connection
        assert raw is not None
        session_start = bundle.info.date
        sql = f"COPY telemetry_samples ({', '.join(_COPY_COLUMNS)}) FROM STDIN"
        with raw.cursor() as cur, cur.copy(sql) as copy:      # type: ignore[attr-defined]
            for (driver, lap_number), frame in bundle.telemetry.items():
                lap_id = lap_ids[(driver, lap_number)]
                start = starts.get((driver, lap_number))
                base = session_start + timedelta(seconds=start if start is not None
                                                 else lap_number * _FALLBACK_LAP_SLOT_S)
                cols = [getattr(frame, name).tolist() for name in TELEMETRY_COLUMNS]
                times = cols[0]
                for i in range(len(frame)):
                    t = times[i]
                    ts = base + timedelta(seconds=t if t == t else 0.0)     # NaN time: the lap start
                    copy.write_row((ts, lap_id, i, *(col[i] for col in cols)))

    # ------------------------------------------------------------ read
    def get_session_info(self, key: str) -> SessionInfo:
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            info = conn.execute(select(sessions.c.info).where(sessions.c.id == sid)).scalar_one()
        return SessionInfo.model_validate(info)

    def list_drivers(self, key: str) -> list[DriverInfo]:
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            rows = conn.execute(select(drivers.c.data).where(drivers.c.session_id == sid).order_by(drivers.c.ord)).scalars()
            return [DriverInfo.model_validate(r) for r in rows]

    def list_laps(self, key: str, driver: str | None = None) -> list[LapRecord]:
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            query = select(laps.c.data).where(laps.c.session_id == sid)
            if driver is not None:
                query = query.where((laps.c.driver == driver.upper()) | (laps.c.driver_number == driver))
            rows = conn.execute(query.order_by(laps.c.ord)).scalars()
            return [LapRecord.model_validate(r) for r in rows]

    def get_telemetry(self, key: str, driver: str, lap: int) -> TelemetryFrame:
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            _check_driver(driver)
            row = conn.execute(select(laps.c.id, laps.c.telemetry_stored).where(
                laps.c.session_id == sid, laps.c.driver == driver, laps.c.lap_number == int(lap))).first()
            if row is None or not row.telemetry_stored:
                raise TelemetryUnavailableError(f"No stored telemetry for {driver} lap {lap} in {key}.")
            cols = [telemetry_samples.c[name] for name in TELEMETRY_COLUMNS]
            data = conn.execute(select(*cols).where(telemetry_samples.c.lap_id == row.id)
                                .order_by(telemetry_samples.c.sample_idx)).all()
        if not data:
            return TelemetryFrame.empty()
        columns = list(zip(*data, strict=True))
        return TelemetryFrame.from_columns({name: np.asarray(columns[i]) for i, name in enumerate(TELEMETRY_COLUMNS)})

    def get_corners(self, key: str) -> list[CornerInfo]:
        names = ("number", "letter", "distance_m", "x", "y", "angle")
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            rows = conn.execute(select(*(corners.c[n] for n in names)).where(corners.c.session_id == sid)
                                .order_by(corners.c.ord)).mappings().all()
        return [CornerInfo.model_validate(dict(r)) for r in rows]

    def get_weather(self, key: str) -> list[WeatherSample]:
        names = ("time_s", "air_temp_c", "track_temp_c", "humidity", "rainfall", "wind_kph")
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            rows = conn.execute(select(*(weather.c[n] for n in names)).where(weather.c.session_id == sid)
                                .order_by(weather.c.ord)).mappings().all()
        return [WeatherSample.model_validate(dict(r)) for r in rows]

    def get_corners_raw(self, key: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            sid = self._require(conn, key)
            raw: list[dict[str, Any]] = conn.execute(select(sessions.c.corners_raw)
                                                     .where(sessions.c.id == sid)).scalar_one()
        return raw

    def list_sessions(self, year: int | None = None) -> list[SessionInfo]:
        query = select(sessions.c.info).where(sessions.c.schema_version == SCHEMA_VERSION)
        if year is not None:
            query = query.where(sessions.c.year == int(year))
        with self.engine.connect() as conn:
            return [SessionInfo.model_validate(r) for r in conn.execute(query.order_by(sessions.c.key)).scalars()]

    # ------------------------------------------------------------ schedule cache
    def save_events(self, year: int, events: list[EventInfo]) -> None:
        values = {"year": int(year), "fetched_at": datetime.now(timezone.utc), "schema_version": SCHEMA_VERSION,
                  "events": [_json(e) for e in events]}
        stmt = pg_insert(event_schedules).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=[event_schedules.c.year], set_={
            k: stmt.excluded[k] for k in ("fetched_at", "schema_version", "events")})
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_events(self, year: int) -> tuple[datetime, list[EventInfo]] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(event_schedules).where(event_schedules.c.year == int(year))).mappings().first()
        if row is None or row["schema_version"] != SCHEMA_VERSION:
            return None
        return row["fetched_at"], [EventInfo.model_validate(e) for e in row["events"]]
