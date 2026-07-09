"""Tire-degradation endpoint (Person 3)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..clients.openf1 import OpenF1Error
from ..models.schemas import TireAnalysis
from ..services import tires as tires_svc

router = APIRouter(prefix="/tires", tags=["tires"])


@router.get("/degradation", response_model=TireAnalysis)
async def degradation(session_key: int, driver_number: int, horizon: int = 10):
    try:
        return await tires_svc.analyse_current_stint(session_key, driver_number, horizon)
    except OpenF1Error as e:
        raise HTTPException(status_code=502, detail=str(e))
