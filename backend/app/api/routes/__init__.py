"""All /api/v1 routes. Each handler parses input, calls one service method, returns."""
from __future__ import annotations

from fastapi import APIRouter

from ..schemas.common import error_responses
from . import comparisons, meta, seasons, sessions

API_V1_PREFIX = "/api/v1"

TAGS_METADATA = [
    {"name": "v1: meta", "description": "Liveness and readiness probes."},
    {"name": "v1: seasons", "description": "Seasons and Grand Prix schedules (FastF1 event schedule, cached per year)."},
    {"name": "v1: sessions", "description": "Ingestion and stored session data: drivers, laps, fastest laps, corners."},
    {"name": "v1: comparisons", "description": "Distance-aligned two-lap comparison with time delta and verified findings. "
                                               "Sign convention: positive delta / time loss = the target lost time."},
]

# Any v1 route can be rate limited or fail unexpectedly; document both once here.
router = APIRouter(prefix=API_V1_PREFIX, responses=error_responses(429, 500))
router.include_router(meta.router)
router.include_router(seasons.router)
router.include_router(sessions.router)
router.include_router(comparisons.router)
