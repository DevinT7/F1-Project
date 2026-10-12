from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from ..deps import get_session_service
from ..schemas.common import error_responses
from ..schemas.sessions import EventsResponse, SeasonsResponse
from ..services import SessionQueryService

router = APIRouter(tags=["v1: seasons"])


@router.get("/seasons", response_model=SeasonsResponse, summary="Available seasons",
            description="Seasons FastF1 can serve (2018 onward), newest first, with how many sessions "
                        "of each are already ingested locally.")
def list_seasons(service: SessionQueryService = Depends(get_session_service)) -> SeasonsResponse:
    return service.seasons()


@router.get("/seasons/{year}/events", response_model=EventsResponse, summary="Grands Prix of a season",
            description="Round, name, country, location, format and per-session UTC dates. The schedule is "
                        "fetched from FastF1 once and cached (past seasons forever, the current one for 24 h).",
            responses=error_responses(404, 422))
def list_events(
    year: int = Path(ge=2018, le=2100, examples=[2024]),
    service: SessionQueryService = Depends(get_session_service),
) -> EventsResponse:
    return service.events(year)
