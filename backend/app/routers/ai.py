"""AI race engineer endpoint (Person 3 / Person 4)."""
from __future__ import annotations

from fastapi import APIRouter

from ..models.schemas import AIReport, AIReportRequest
from ..services import ai_engineer

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/report", response_model=AIReport)
async def report(req: AIReportRequest):
    return await ai_engineer.build_report(req)
