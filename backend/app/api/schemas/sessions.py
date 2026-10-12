"""Response schemas for seasons, events, sessions, drivers, laps and corners.

Domain models from app.ingestion.models (SessionInfo, DriverInfo, LapRecord, ...) are
reused as-is so the API and the stored data can never drift apart.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ...ingestion.models import CornerInfo, DriverInfo, EventInfo, LapRecord, Manifest, SessionInfo


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Season(_Out):
    year: int
    sessions_stored: int = Field(description="sessions of this season already ingested locally")


class SeasonsResponse(_Out):
    seasons: list[Season]
    earliest_supported: int = Field(description="FastF1 has timing + telemetry from 2018; only 2024 is verified")


class EventsResponse(_Out):
    year: int
    events: list[EventInfo]


class SessionStatusResponse(_Out):
    session_key: str = Field(examples=["2024-05-Q"])
    status: Literal["ready", "not_ingested", "stale_schema"] = Field(
        description="ready = stored with the current schema; stale_schema = stored by an older version, re-ingest"
    )
    info: SessionInfo | None = None
    manifest: Manifest | None = None


class IngestResponse(_Out):
    session_key: str
    skipped: bool = Field(description="true when the session was already stored (no network calls made)")
    laps: int
    telemetry_laps: int
    valid_laps: int
    elapsed_s: float


class DriversResponse(_Out):
    session_key: str
    drivers: list[DriverInfo]


class LapsResponse(_Out):
    session_key: str
    driver: str | None = None
    laps: list[LapRecord]


class FastestLap(_Out):
    driver: str
    team: str | None = None
    team_color: str | None = None
    lap: LapRecord | None = Field(description="null when the driver has no lap valid for comparison")


class FastestLapsResponse(_Out):
    session_key: str
    laps: list[FastestLap] = Field(description="one entry per driver, fastest first; drivers without a valid lap last")


class CornersResponse(_Out):
    session_key: str
    circuit_rotation_deg: float | None = None
    corners: list[CornerInfo]


class HealthResponse(_Out):
    status: Literal["ok"]
    version: str


class ReadyResponse(_Out):
    status: Literal["ready", "not_ready"]
    checks: dict[str, bool]
