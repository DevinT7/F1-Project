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
    # Opt-in FastF1 distance-native path (older seasons / deep single-lap analysis).
    # All four must be supplied together, or the OpenF1 integration path is used.
    prefer_fastf1: bool = False,
    year: int | None = None,
    gp: str | None = None,
    session_name: str | None = None,
    driver_code: str | None = None,
    reference_driver_code: str | None = None,
):
    # Same driver, same (unresolved) lap selection -> this would resolve to
    # literally the same lap on both sides, a meaningless comparison. Reject
    # explicitly and fail fast, rather than doing the work and returning a
    # degenerate but "successful" all-zero LapComparison. Comparing two
    # *different* laps from the same driver (e.g. before/after a setup change)
    # is a legitimate use and stays allowed.
    if driver_number == reference_driver_number and lap_number == reference_lap_number:
        raise HTTPException(status_code=400, detail=(
            "driver_number and reference_driver_number resolve to the same lap "
            "(same driver, same lap_number) -- nothing to compare. Pick a "
            "different reference_lap_number to compare two laps from one driver."
        ))

    driver_meta_tuple = (
        (year, gp, session_name, driver_code)
        if prefer_fastf1 and all((year, gp, session_name, driver_code)) else None
    )
    ref_meta_tuple = (
        (year, gp, session_name, reference_driver_code)
        if prefer_fastf1 and all((year, gp, session_name, reference_driver_code)) else None
    )
    # Preferred known-corner source (FastF1 circuit info) needs only year/gp/session,
    # not a driver code, so it's opt-in independently of whether either trace call
    # actually used the FastF1 telemetry path.
    corners_meta = (
        (year, gp, session_name)
        if prefer_fastf1 and all((year, gp, session_name)) else None
    )

    try:
        driver_trace = await telemetry_svc.build_trace(
            session_key, driver_number, lap_number,
            prefer_fastf1=prefer_fastf1, fastf1_meta=driver_meta_tuple)
        ref_trace = await telemetry_svc.build_trace(
            session_key, reference_driver_number, reference_lap_number,
            prefer_fastf1=prefer_fastf1, fastf1_meta=ref_meta_tuple)
        driver_meta = await sessions_svc.get_driver(session_key, driver_number)
        ref_meta = await sessions_svc.get_driver(session_key, reference_driver_number)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not driver_trace.samples or not ref_trace.samples:
        raise HTTPException(status_code=404, detail="No telemetry for one or both laps.")

    # Real sector times (for sector_deltas_from_laps): fetch by the trace's own
    # lap_number so the row always matches the telemetry actually used, even when
    # lap_number defaulted to the fastest lap. Best-effort -- if this comes back
    # empty (e.g. the FastF1 path was used, or OpenF1 is briefly unavailable),
    # compare_traces falls back to deriving sector deltas from the delta trace.
    driver_lap = await telemetry_svc.get_lap_row(
        session_key, driver_number, driver_trace.lap_number)
    ref_lap = await telemetry_svc.get_lap_row(
        session_key, reference_driver_number, ref_trace.lap_number)

    # Preferred corner source: FastF1 circuit info, when opted in and available;
    # get_known_corners never raises, so an unavailable/erroring circuit lookup
    # degrades gracefully to detect_corners' speed-minima fallback (known=None).
    known_corners = await analysis_svc.get_known_corners(corners_meta)

    return analysis_svc.compare_traces(
        driver_meta, ref_meta, driver_trace, ref_trace,
        driver_lap=driver_lap, ref_lap=ref_lap,
        corners_known=known_corners,
        include_traces=include_traces,
    )
