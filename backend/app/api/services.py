"""Services behind the /api/v1 routes. Routes stay thin: parse, call one method, return.

SessionQueryService wraps the repository + ingestion service; ComparisonService runs
Phase 2 (alignment, at 1 m) and Phase 3 (findings), thins the arrays for transport and
memoizes results in an in-process LRU (replaced by Redis in Phase 8).
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from ..ingestion.errors import (
    DriverNotFoundError,
    LapInvalidError,
    LapNotFoundError,
    PayloadTooLargeError,
    SessionNotFoundError,
)
from ..ingestion.models import SCHEMA_VERSION, DriverInfo, FloatArray, IntArray, LapRecord
from ..ingestion.service import IngestionService, SessionSource, fastest_valid_lap
from ..processing.delta import compare_laps
from ..processing.insights import build_session_context, generate_findings
from ..processing.models import AlignedLap, LapData
from ..storage.repository import SessionRepository
from ..tires.config import ExclusionConfig
from ..tires.stints import summarize_stints
from .schemas.common import canonical_session_key, public_session_key
from .schemas.comparisons import (
    ComparisonChannels,
    ComparisonMeta,
    ComparisonRequest,
    ComparisonResponse,
    LapChannels,
    LapMeta,
    LapSelector,
)
from .schemas.sessions import (
    CornersResponse,
    DriversResponse,
    EventsResponse,
    FastestLap,
    FastestLapsResponse,
    IngestResponse,
    LapsResponse,
    Season,
    SeasonsResponse,
    SessionStatusResponse,
    StintsResponse,
)

EARLIEST_SEASON = 2018
ANALYSIS_STEP_M = 1.0
MAX_POINTS = 15_000          # transport cap; the longest circuit (Spa, ~7 km) at 1 m is ~7,000
CACHE_SIZE = 64
SESSION_TABLES_SIZE = 8


def _find_driver(drivers: list[DriverInfo], driver: str) -> DriverInfo | None:
    wanted = driver.strip().upper()
    return next((d for d in drivers if d.code.upper() == wanted or d.number == driver.strip()), None)


class SessionQueryService:
    def __init__(self, repo: SessionRepository, source_factory: Callable[[], SessionSource]) -> None:
        self.repo = repo
        self._source_factory = source_factory

    def _ingestion(self) -> IngestionService:
        return IngestionService(self.repo, self._source_factory())

    def _require(self, session_key: str) -> str:
        key = canonical_session_key(session_key)
        if not self.repo.has_session(key):
            raise SessionNotFoundError(f"Session {public_session_key(key)} is not ingested; POST .../ingest first.")
        return key

    def seasons(self) -> SeasonsResponse:
        stored: dict[int, int] = {}
        for info in self.repo.list_sessions():
            stored[info.year] = stored.get(info.year, 0) + 1
        current = datetime.now(timezone.utc).year
        years = sorted(set(range(EARLIEST_SEASON, current + 1)) | set(stored), reverse=True)
        return SeasonsResponse(
            seasons=[Season(year=y, sessions_stored=stored.get(y, 0)) for y in years],
            earliest_supported=EARLIEST_SEASON,
        )

    def events(self, year: int) -> EventsResponse:
        return EventsResponse(year=year, events=self._ingestion().list_events(year))

    def status(self, session_key: str) -> SessionStatusResponse:
        key = canonical_session_key(session_key)
        manifest = self.repo.get_manifest(key)
        if manifest is None:
            return SessionStatusResponse(session_key=public_session_key(key), status="not_ingested")
        if manifest.schema_version != SCHEMA_VERSION:
            return SessionStatusResponse(session_key=public_session_key(key), status="stale_schema", manifest=manifest)
        return SessionStatusResponse(
            session_key=public_session_key(key), status="ready",
            info=self.repo.get_session_info(key), manifest=manifest,
        )

    def ingest(self, session_key: str, *, force: bool = False) -> IngestResponse:
        key = canonical_session_key(session_key)
        year, rnd, code = key.split("_")
        result = self._ingestion().ingest(int(year), int(rnd), code, force=force)
        return IngestResponse(
            session_key=public_session_key(result.key), skipped=result.skipped, laps=result.laps,
            telemetry_laps=result.telemetry_laps, valid_laps=result.valid_laps, elapsed_s=round(result.elapsed_s, 3),
        )

    def drivers(self, session_key: str) -> DriversResponse:
        key = self._require(session_key)
        return DriversResponse(session_key=public_session_key(key), drivers=self.repo.list_drivers(key))

    def laps(self, session_key: str, driver: str | None) -> LapsResponse:
        key = self._require(session_key)
        if driver is not None and _find_driver(self.repo.list_drivers(key), driver) is None:
            raise DriverNotFoundError(f"Driver {driver} is not in session {public_session_key(key)}.")
        return LapsResponse(session_key=public_session_key(key), driver=driver, laps=self.repo.list_laps(key, driver))

    def fastest_laps(self, session_key: str) -> FastestLapsResponse:
        key = self._require(session_key)
        best: dict[str, LapRecord] = {}
        for lap in self.repo.list_laps(key):
            if lap.is_valid_for_comparison and lap.lap_time_s is not None:
                current = best.get(lap.driver)
                if current is None or (lap.lap_time_s, lap.lap_number) < (current.lap_time_s or math.inf, current.lap_number):
                    best[lap.driver] = lap
        entries = [
            FastestLap(driver=d.code, team=d.team, team_color=d.team_color, lap=best.get(d.code))
            for d in self.repo.list_drivers(key)
        ]
        entries.sort(key=lambda e: (e.lap is None, e.lap.lap_time_s if e.lap else 0.0, e.driver))
        return FastestLapsResponse(session_key=public_session_key(key), laps=entries)

    def stints(self, session_key: str) -> StintsResponse:
        key = self._require(session_key)
        cfg = ExclusionConfig()
        return StintsResponse(session_key=public_session_key(key), min_clean_laps=cfg.min_clean_stint_laps,
                              drivers=summarize_stints(self.repo.list_laps(key), cfg))

    def corners(self, session_key: str) -> CornersResponse:
        key = self._require(session_key)
        return CornersResponse(
            session_key=public_session_key(key),
            circuit_rotation_deg=self.repo.get_session_info(key).circuit_rotation_deg,
            corners=self.repo.get_corners(key),
        )


def _tolist(values: NDArray[Any]) -> list[Any]:
    """1-D numpy array → plain Python list (typed for mypy; numpy's stub is a broad union)."""
    return cast(list[Any], values.tolist())


def _floats(values: NDArray[Any], idx: IntArray, ndigits: int) -> list[float | None]:
    picked = np.round(values[idx].astype(np.float64), ndigits)
    finite = np.isfinite(picked)
    out: list[float | None] = _tolist(picked)
    if not finite.all():  # NaN/inf → null, never NaN in JSON
        for i in np.flatnonzero(~finite):
            out[i] = None
    return out


def _channels(lap: AlignedLap, idx: IntArray) -> LapChannels:
    return LapChannels(
        time_s=_tolist(np.round(lap.time_s[idx], 4)),
        speed_kph=_floats(lap.speed_kph, idx, 2),
        throttle_pct=_floats(lap.throttle_pct, idx, 1),
        brake=_tolist(lap.brake[idx]),
        gear=_tolist(lap.gear[idx]),
        rpm=_floats(lap.rpm, idx, 0),
        drs_open=_tolist(lap.drs_open[idx]),
        x=_floats(lap.x, idx, 1),
        y=_floats(lap.y, idx, 1),
    )


def transport_indices(grid_m: FloatArray, resolution_m: float) -> IntArray:
    """Grid indices closest to every resolution_m step, always including both ends."""
    step = float(grid_m[1] - grid_m[0]) if len(grid_m) > 1 else 1.0
    targets = np.arange(0.0, float(grid_m[-1]), resolution_m)
    idx = np.clip(np.round(targets / step).astype(np.int64), 0, len(grid_m) - 1)
    out: IntArray = np.unique(np.concatenate((idx, [len(grid_m) - 1])))
    return out


@dataclass(frozen=True)
class _SessionTables:
    ingested_at: datetime
    drivers: list[DriverInfo]
    laps: list[LapRecord]


class ComparisonService:
    """Thread-safe. Two in-process LRUs: per-session lap/driver tables (so resolving
    "fastest" never re-reads Parquet) and finished comparisons, stored as model + the
    JSON a cache hit returns. Keys include the manifest's ingested_at, so a re-ingest
    invalidates both."""

    def __init__(self, repo: SessionRepository, cache_size: int = CACHE_SIZE) -> None:
        self.repo = repo
        self.cache_size = cache_size
        self._cache: OrderedDict[tuple[object, ...], tuple[ComparisonResponse, str]] = OrderedDict()
        self._tables: OrderedDict[str, _SessionTables] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def _session_tables(self, key: str) -> _SessionTables:
        manifest = self.repo.get_manifest(key)
        if manifest is None or not self.repo.has_session(key):
            raise SessionNotFoundError(f"Session {public_session_key(key)} is not ingested; POST .../ingest first.")
        with self._lock:
            tables = self._tables.get(key)
            if tables is not None and tables.ingested_at == manifest.ingested_at:
                self._tables.move_to_end(key)
                return tables
        tables = _SessionTables(manifest.ingested_at, self.repo.list_drivers(key), self.repo.list_laps(key))
        with self._lock:
            self._tables[key] = tables
            while len(self._tables) > SESSION_TABLES_SIZE:
                self._tables.popitem(last=False)
        return tables

    def _resolve(self, key: str, selector: LapSelector, tables: _SessionTables) -> tuple[LapRecord, DriverInfo | None]:
        info = _find_driver(tables.drivers, selector.driver)
        wanted = selector.driver.strip().upper()
        laps = [lap for lap in tables.laps if lap.driver.upper() == wanted or lap.driver_number == selector.driver.strip()]
        if not laps:
            raise DriverNotFoundError(f"Driver {selector.driver} has no laps in {public_session_key(key)}.")
        if selector.lap == "fastest":
            return fastest_valid_lap(laps, laps[0].driver, public_session_key(key)), info
        match = next((lap for lap in laps if lap.lap_number == selector.lap), None)
        if match is None:
            raise LapNotFoundError(f"{laps[0].driver} has no lap {selector.lap} in {public_session_key(key)}.")
        if not match.is_valid_for_comparison:
            raise LapInvalidError(
                f"{match.driver} lap {match.lap_number} is not valid for comparison ({match.invalid_reason})."
            )
        return match, info

    def _lookup(self, request: ComparisonRequest) -> tuple[tuple[object, ...], tuple[ComparisonResponse, str] | None, Callable[[], ComparisonResponse]]:
        key = canonical_session_key(request.session_key)
        tables = self._session_tables(key)
        ref_rec, ref_driver = self._resolve(key, request.reference, tables)
        tgt_rec, tgt_driver = self._resolve(key, request.target, tables)
        cache_key = (key, tables.ingested_at, ref_rec.driver, ref_rec.lap_number,
                     tgt_rec.driver, tgt_rec.lap_number, float(request.resolution_m))
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit is not None:
                self._cache.move_to_end(cache_key)
                self.hits += 1
            else:
                self.misses += 1
        return cache_key, hit, lambda: self._build(key, request.resolution_m, ref_rec, ref_driver, tgt_rec, tgt_driver)

    def _store(self, cache_key: tuple[object, ...], response: ComparisonResponse) -> str:
        cached = response.model_copy(update={"meta": response.meta.model_copy(update={"cached": True})})
        with self._lock:
            self._cache[cache_key] = (cached, cached.model_dump_json())
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return response.model_dump_json()

    def compare(self, request: ComparisonRequest) -> ComparisonResponse:
        cache_key, hit, build = self._lookup(request)
        if hit is not None:
            return hit[0]
        response = build()
        self._store(cache_key, response)
        return response

    def compare_json(self, request: ComparisonRequest) -> str:
        """Same as compare(), already serialized: a cache hit costs no serialization at all."""
        cache_key, hit, build = self._lookup(request)
        if hit is not None:
            return hit[1]
        return self._store(cache_key, build())

    def _build(
        self, key: str, resolution_m: float,
        ref_rec: LapRecord, ref_driver: DriverInfo | None, tgt_rec: LapRecord, tgt_driver: DriverInfo | None,
    ) -> ComparisonResponse:
        cmp = compare_laps(
            LapData.from_repository(self.repo, key, ref_rec.driver, ref_rec.lap_number),
            LapData.from_repository(self.repo, key, tgt_rec.driver, tgt_rec.lap_number),
            step=ANALYSIS_STEP_M,
        )
        info = self.repo.get_session_info(key)
        findings = generate_findings(cmp, self.repo.get_corners(key),
                                     context=build_session_context(ref_rec, tgt_rec, info))
        idx = transport_indices(cmp.grid_m, resolution_m)
        if len(idx) > MAX_POINTS:
            raise PayloadTooLargeError(f"{len(idx)} points exceeds the {MAX_POINTS}-point cap; use a coarser resolution.")

        def meta(rec: LapRecord, driver: DriverInfo | None) -> LapMeta:
            return LapMeta(
                driver=rec.driver, driver_number=rec.driver_number,
                full_name=driver.full_name if driver else None, team=driver.team if driver else rec.team,
                team_color=driver.team_color if driver else None, lap_number=rec.lap_number,
                lap_time_s=rec.lap_time_s or 0.0, sector_s=list(rec.sector_s),
                compound=rec.compound, tyre_age_laps=rec.tyre_age,
            )

        delta: list[float] = _tolist(np.round(cmp.delta_s[idx], 4))
        delta[0] = 0.0
        return ComparisonResponse(
            meta=ComparisonMeta(
                session_key=public_session_key(key), resolution_m=resolution_m, points=len(idx),
                reference=meta(ref_rec, ref_driver), target=meta(tgt_rec, tgt_driver),
                quality=findings.quality, cached=False,
            ),
            distance_m=_tolist(np.round(cmp.grid_m[idx], 3)),
            channels=ComparisonChannels(reference=_channels(cmp.reference, idx), target=_channels(cmp.target, idx)),
            delta_s=delta,
            findings=findings,
        )
