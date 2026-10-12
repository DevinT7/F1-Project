"""Orchestrates fetch (FastF1 adapter) → normalize → store (repository)."""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import pandas as pd
import structlog

from . import normalize as N
from .errors import IngestionError, LapInvalidError, TelemetryUnavailableError
from .fastf1_client import RawSession
from .models import EventInfo, LapKey, LapRecord, SessionBundle, TelemetryFrame
from ..storage.repository import SessionRepository

log = structlog.get_logger("raceiq.ingestion")

# Schedules for the current/future seasons change (postponements, sprint formats);
# past seasons are frozen and cached forever.
SCHEDULE_TTL = timedelta(hours=24)


class SessionSource(Protocol):
    """What the service needs from the FastF1 adapter (mocked in tests)."""

    @property
    def fastf1_version(self) -> str: ...
    def load_session(self, year: int, round_number: int, session_code: str) -> RawSession: ...
    def lap_telemetry(self, raw: RawSession, driver: str, lap_number: int) -> pd.DataFrame: ...
    def event_schedule(self, year: int) -> pd.DataFrame: ...


@dataclass(frozen=True)
class IngestResult:
    key: str
    skipped: bool
    laps: int = 0
    telemetry_laps: int = 0
    valid_laps: int = 0
    elapsed_s: float = 0.0
    error: str | None = None


class IngestionService:
    def __init__(
        self,
        repo: SessionRepository,
        source: SessionSource,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.repo = repo
        self.source = source
        self.clock = clock

    def ingest(self, year: int, round_number: int, session: str, *, force: bool = False) -> IngestResult:
        started = time.monotonic()
        key = N.make_session_key(year, round_number, session)
        if not force and self.repo.has_session(key):
            log.info("ingest.skip", key=key, reason="present with current schema")
            return IngestResult(key=key, skipped=True, elapsed_s=time.monotonic() - started)

        log.info("ingest.load", key=key)
        raw = self.source.load_session(year, round_number, session)
        bundle = self.build_bundle(raw)
        self.repo.save_session(bundle)
        result = IngestResult(
            key=key,
            skipped=False,
            laps=len(bundle.laps),
            telemetry_laps=len(bundle.telemetry),
            valid_laps=sum(lap.is_valid_for_comparison for lap in bundle.laps),
            elapsed_s=time.monotonic() - started,
        )
        log.info("ingest.done", key=key, laps=result.laps, telemetry_laps=result.telemetry_laps,
                 valid_laps=result.valid_laps, elapsed_s=round(result.elapsed_s, 1))
        return result

    def build_bundle(self, raw: RawSession) -> SessionBundle:
        info = N.normalize_session_info(raw.info)
        weather = N.normalize_weather(raw.weather)
        # First pass only to learn which laps exist; validity needs telemetry stats.
        lap_keys = [(lap.driver, lap.lap_number) for lap in N.normalize_laps(raw.laps, info.key)]

        telemetry: dict[LapKey, TelemetryFrame] = {}
        stats: dict[LapKey, tuple[int, float | None]] = {}
        missing = 0
        for i, (driver, lap_number) in enumerate(lap_keys, start=1):
            try:
                frame = N.normalize_telemetry(self.source.lap_telemetry(raw, driver, lap_number))
            except TelemetryUnavailableError as exc:
                missing += 1
                log.debug("ingest.telemetry_missing", key=info.key, driver=driver, lap=lap_number, error=str(exc))
                continue
            if len(frame):
                telemetry[(driver, lap_number)] = frame
                stats[(driver, lap_number)] = N.telemetry_stats(frame)
            if i % 50 == 0 or i == len(lap_keys):
                log.info("ingest.telemetry", key=info.key, done=i, total=len(lap_keys))
        if missing:
            log.warning("ingest.telemetry_missing_total", key=info.key, missing=missing, total=len(lap_keys))

        return SessionBundle(
            info=info,
            drivers=N.normalize_drivers(raw.results),
            laps=N.normalize_laps(raw.laps, info.key, weather=weather, telemetry_stats=stats),
            weather=weather,
            corners=N.normalize_corners(raw.corners),
            telemetry=telemetry,
            corners_raw=N.raw_records(raw.corners),
            fastf1_version=raw.fastf1_version,
        )

    # -- schedule
    def list_events(self, year: int, *, refresh: bool = False) -> list[EventInfo]:
        now = self.clock()
        cached = None if refresh else self.repo.get_events(year)
        if cached is not None:
            fetched_at, events = cached
            if year < now.year or now - fetched_at < SCHEDULE_TTL:
                return events
        events = N.normalize_schedule(self.source.event_schedule(year), year)
        self.repo.save_events(year, events)
        return events

    def ingest_season(
        self,
        year: int,
        sessions: Sequence[str] = ("Q", "R"),
        *,
        force: bool = False,
    ) -> list[IngestResult]:
        """Ingest the given sessions for every round that has already happened.
        One failing session is logged and reported, not fatal to the batch."""
        wanted = [N.normalize_session_code(code) for code in sessions]
        now = self.clock()
        results: list[IngestResult] = []
        for event in self.list_events(year):
            for session in event.sessions:
                if session.code not in wanted:
                    continue
                if session.date is not None and session.date > now:
                    continue
                try:
                    results.append(self.ingest(year, event.round, session.code, force=force))
                except Exception as exc:  # one bad session must not sink a season batch
                    key = N.make_session_key(year, event.round, session.code)
                    log.error("ingest.failed", key=key, error=str(exc),
                              expected=isinstance(exc, IngestionError))
                    results.append(IngestResult(key=key, skipped=False, error=str(exc)))
        return results


def fastest_valid_lap(laps: Sequence[LapRecord], driver: str, key: str) -> LapRecord:
    """Lowest lap_time_s among `laps` (one driver's) that are valid for comparison."""
    candidates = [lap for lap in laps if lap.is_valid_for_comparison and lap.lap_time_s is not None]
    if not candidates:
        raise LapInvalidError(f"{driver} has no lap valid for comparison in {key}.")
    return min(candidates, key=lambda lap: (lap.lap_time_s, lap.lap_number))


def get_fastest_valid_lap(repo: SessionRepository, key: str, driver: str) -> LapRecord:
    """Lowest lap_time_s among the driver's laps that are valid for comparison."""
    return fastest_valid_lap(repo.list_laps(key, driver), driver, key)
