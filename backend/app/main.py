"""RaceIQ backend entrypoint (Person 1).

Run locally:
    cd backend
    pip install -r requirements.txt
    uvicorn app.main:app --reload
Then open http://localhost:8000/docs
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import settings
from .routers import ai, analysis, sessions, tires

app = FastAPI(
    title="RaceIQ API",
    version=__version__,
    description="AI-powered Formula 1 performance analyzer. Data: OpenF1 + FastF1.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

API = "/api"
app.include_router(sessions.router, prefix=API)
app.include_router(analysis.router, prefix=API)
app.include_router(tires.router, prefix=API)
app.include_router(ai.router, prefix=API)


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "version": __version__,
            "fastf1_enabled": settings.enable_fastf1,
            "ai": "llm" if settings.openai_api_key else "template"}
