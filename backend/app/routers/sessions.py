"""Sessions, drivers, telemetry endpoints (Person 1)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..clients.openf1 import OpenF1Error
from ..models.schemas import Driver, Session, TelemetryTrace
from ..services import sessions as sessions_svc
from ..services import telemetry as telemetry_svc

router = APIRouter(tags=["sessions"])


@router.get("/sessions", response_model=list[Session])
async def get_sessions(
    year: int | None = Query(None, description="e.g. 2024"),
    country: str | None = Query(None, description="country_name, e.g. Italy"),
    session_name: str | None = Query(None, description="Race / Qualifying / Sprint / Practice"),
):
    try:
        return await sessions_svc.list_sessions(year, country, session_name)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/sessions/{session_key}/drivers", response_model=list[Driver])
async def get_drivers(session_key: int):
    try:
        return await sessions_svc.list_drivers(session_key)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/telemetry", response_model=TelemetryTrace)
async def get_telemetry(
    session_key: int,
    driver_number: int,
    lap_number: int | None = Query(None, description="omit for the driver's fastest lap"),
):
    try:
        return await telemetry_svc.build_trace(session_key, driver_number, lap_number)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))
