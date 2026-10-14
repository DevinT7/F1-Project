"""All /api/v1 routes. Each handler parses input, calls one service method, returns."""
from __future__ import annotations

from fastapi import APIRouter

from ..schemas.common import error_responses
from . import auth, comparisons, engineer, jobs, meta, seasons, sessions, tires

API_V1_PREFIX = "/api/v1"

TAGS_METADATA = [
    {"name": "v1: meta", "description": "Liveness and readiness probes."},
    {"name": "v1: seasons", "description": "Seasons and Grand Prix schedules (FastF1 event schedule, cached per year)."},
    {"name": "v1: sessions", "description": "Ingestion and stored session data: drivers, laps, fastest laps, corners."},
    {"name": "v1: comparisons", "description": "Distance-aligned two-lap comparison with time delta and verified findings. "
                                               "Sign convention: positive delta / time loss = the target lost time."},
    {"name": "v1: tires", "description": "Tyre forecasts and a heuristic pit window from the registered production "
                                         "model, using only laps up to the requested lap."},
    {"name": "v1: auth", "description": "Accounts (argon2 passwords), JWT access tokens, the refresh cookie, "
                                        "saved comparisons. Public: comparisons, sessions, tyres. Signed in: the AI "
                                        "engineer. Admin: ingest, training, jobs."},
    {"name": "v1: admin", "description": "Admin-only operations (training)."},
    {"name": "v1: jobs", "description": "Background jobs (ingestion, model training): status and progress."},
    {"name": "v1: engineer", "description": "AI race engineer: validated reports and follow-up Q&A over a comparison's "
                                            "FactSheet. Numbers come from the analysis engine; the AI only explains them."},
]

# Any v1 route can be rate limited or fail unexpectedly; document both once here.
router = APIRouter(prefix=API_V1_PREFIX, responses=error_responses(429, 500))
router.include_router(meta.router)
router.include_router(seasons.router)
router.include_router(sessions.router)
router.include_router(comparisons.router)
router.include_router(tires.router)
router.include_router(engineer.router)
router.include_router(jobs.router)
router.include_router(auth.auth)
router.include_router(auth.me)
router.include_router(auth.admin)
