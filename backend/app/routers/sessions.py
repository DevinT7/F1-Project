"""OpenF1 sessions plus legacy telemetry endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..clients.openf1 import OpenF1Error, get_client
from ..models.schemas import Driver, Session, TelemetryTrace
from ..services import sessions as sessions_svc
from ..services import telemetry as telemetry_svc
from ._openf1 import envelope, provider_http_exception

router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def get_sessions(
    year: int | None = Query(None, ge=1950, le=2100, description="e.g. 2024"),
    country_name: str | None = Query(None, description="OpenF1 country_name filter"),
    country: str | None = Query(None, description="Backward-compatible country_name alias"),
    session_type: str | None = Query(None, description="Race / Qualifying / Practice / Sprint"),
) -> dict[str, Any]:
    if country_name and country and country_name != country:
        raise HTTPException(
            status_code=400,
            detail="Use either country_name or country, not conflicting values.",
        )
    try:
        result = await get_client().get_sessions(
            year=year,
            country_name=country_name or country,
            session_type=session_type,
        )
    except OpenF1Error as exc:
        raise provider_http_exception(exc) from exc
    return envelope(result)


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
