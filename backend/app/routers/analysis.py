"""Lap comparison endpoint (Person 2)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..clients.openf1 import OpenF1Error
from ..models.schemas import LapComparison
from ..services import analysis as analysis_svc
from ..services import sessions as sessions_svc
from ..services import telemetry as telemetry_svc

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/compare", response_model=LapComparison)
async def compare(
    session_key: int,
    driver_number: int,
    reference_driver_number: int,
    lap_number: int | None = None,
    reference_lap_number: int | None = None,
    include_traces: bool = True,
):
    try:
        driver_trace = await telemetry_svc.build_trace(session_key, driver_number, lap_number)
        ref_trace = await telemetry_svc.build_trace(
            session_key, reference_driver_number, reference_lap_number)
        driver_meta = await sessions_svc.get_driver(session_key, driver_number)
        ref_meta = await sessions_svc.get_driver(session_key, reference_driver_number)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not driver_trace.samples or not ref_trace.samples:
        raise HTTPException(status_code=404, detail="No telemetry for one or both laps.")

    return analysis_svc.compare_traces(
        driver_meta, ref_meta, driver_trace, ref_trace,
        include_traces=include_traces,
    )
