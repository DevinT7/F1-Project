"""Telemetry endpoints backed by FastF1."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..clients import fastf1_client

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.get("/status")
async def telemetry_status() -> dict[str, Any]:
    return fastf1_client.status()


@router.get("/lap")
async def get_lap_telemetry(
    year: int = Query(..., ge=1950, le=2100),
    grand_prix: str = Query(..., min_length=1),
    session_type: str = Query(..., alias="session", min_length=1),
    driver: str = Query(..., min_length=2, max_length=4),
    lap: int = Query(..., gt=0),
    sample_rate: str = Query("medium", pattern="^(high|medium|low)$"),
) -> dict[str, Any]:
    try:
        return await fastf1_client.get_client().get_lap_telemetry(
            year=year,
            grand_prix=grand_prix,
            session_type=session_type,
            driver=driver,
            lap_number=lap,
            sample_rate=sample_rate,
        )
    except fastf1_client.FastF1Error as exc:
        raise _fastf1_http_exception(exc) from exc


@router.get("/compare")
async def compare_lap_telemetry(
    year: int = Query(..., ge=1950, le=2100),
    grand_prix: str = Query(..., min_length=1),
    session_type: str = Query(..., alias="session", min_length=1),
    driver_a: str = Query(..., min_length=2, max_length=4),
    lap_a: int = Query(..., gt=0),
    driver_b: str = Query(..., min_length=2, max_length=4),
    lap_b: int = Query(..., gt=0),
    sample_rate: str = Query("medium", pattern="^(high|medium|low)$"),
) -> dict[str, Any]:
    try:
        return await fastf1_client.get_client().compare_lap_telemetry(
            year=year,
            grand_prix=grand_prix,
            session_type=session_type,
            driver_a=driver_a,
            lap_a=lap_a,
            driver_b=driver_b,
            lap_b=lap_b,
            sample_rate=sample_rate,
        )
    except fastf1_client.FastF1Error as exc:
        raise _fastf1_http_exception(exc) from exc


def _fastf1_http_exception(exc: fastf1_client.FastF1Error) -> HTTPException:
    if isinstance(exc, fastf1_client.FastF1DisabledError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, fastf1_client.FastF1TimeoutError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, fastf1_client.FastF1SessionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, fastf1_client.FastF1DriverNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, fastf1_client.FastF1LapNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, fastf1_client.FastF1TelemetryUnavailableError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=502, detail="FastF1 telemetry request failed.")
