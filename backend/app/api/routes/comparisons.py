from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..deps import get_comparison_service
from ..schemas.common import error_responses
from ..schemas.comparisons import SIGN_CONVENTION, ComparisonRequest, ComparisonResponse
from ..services import ComparisonService

router = APIRouter(tags=["v1: comparisons"])


@router.post(
    "/comparisons",
    response_model=ComparisonResponse,
    summary="Compare two laps",
    description=(
        "Aligns the target lap to the reference lap by distance (analysis at 1 m), then returns both laps' "
        "channels, the cumulative time delta and the Phase 3 verified findings. Every array has "
        "`meta.points` entries aligned to `distance_m`; booleans are JSON booleans; missing samples are null. "
        "Either lap may be `\"fastest\"`. Laps not valid for comparison are rejected (422) with the reason.\n\n"
        f"**Sign convention:** {SIGN_CONVENTION}\n\n"
        "Responses are gzip-compressed when the client sends `Accept-Encoding: gzip`, and memoized "
        "in-process (`meta.cached`)."
    ),
    responses=error_responses(404, 413, 422, 424),
)
def create_comparison(
    request: ComparisonRequest,
    http: Request,
    service: ComparisonService = Depends(get_comparison_service),
) -> Response:
    # The service returns JSON rendered from a validated ComparisonResponse (cached hits are
    # pre-rendered, and pre-gzipped for clients that accept it), skipping FastAPI's re-validation +
    # jsonable_encoder pass (~35 ms) and per-request compression (the GZip middleware leaves a
    # response that already has a Content-Encoding alone).
    gzip_ok = "gzip" in http.headers.get("accept-encoding", "").lower()
    body, gzipped = service.compare_body(request, gzip_ok)
    headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"} if gzipped else None
    return Response(content=body, media_type="application/json", headers=headers)
