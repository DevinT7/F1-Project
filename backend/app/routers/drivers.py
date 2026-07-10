"""OpenF1 driver endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..clients.openf1 import OpenF1Error, get_client
from ._openf1 import envelope, provider_http_exception

router = APIRouter(prefix="/drivers", tags=["drivers"])


@router.get("")
async def list_drivers(
    session_key: int | None = Query(None, gt=0),
    driver_number: int | None = Query(None, gt=0),
) -> dict[str, Any]:
    if session_key is None:
        raise HTTPException(status_code=400, detail="session_key is required.")
    try:
        result = await get_client().get_drivers(
            session_key=session_key,
            driver_number=driver_number,
        )
    except OpenF1Error as exc:
        raise provider_http_exception(exc) from exc
    return envelope(result)
