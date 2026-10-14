from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse

from ...ai.metrics import METRICS
from ...config import settings
from ...auth.deps import bearer, current_user, engineer_limit, get_account_store, require_admin
from ...auth.store import AccountStore
from ..deps import get_engineer_service
from ..engineer_service import EngineerService, FactSheetNotFoundError
from ..errors import error_response, request_id
from ..schemas.common import error_responses
from ..schemas.engineer import (
    FACTSHEET_HASH_PATTERN, MAX_QUESTION_CHARS, EngineerAnswer, EngineerAskRequest, EngineerReport,
    EngineerReportRequest, FactsResponse,
)

# signed-in users only (LLM cost control), with a per-user token bucket; the admin metrics are separate
router = APIRouter(prefix="/engineer", tags=["v1: engineer"])
SIGNED_IN = [Depends(engineer_limit)]

TRANSPARENCY = ("Every number is computed by the analysis engine; the AI only phrases the verified facts. "
                "`generated_by` says who wrote the text (llm | template | router), `outcome` how it got there "
                "(a template is served when the LLM is disabled, unavailable or its text failed validation, "
                "with `fallback_reason`), `facts` holds every cited fact and `segments` maps fact prefixes to "
                "finding ids. Non-streaming: nothing reaches the client before it passes the validator.")


@router.post("/report", response_model=EngineerReport, summary="Race engineer report for a comparison",
             description="Builds the FactSheet for the comparison (plus the target's tyre forecast as of a lap when "
                         "`tire_context` is given, races only) and returns the validated report. Cached by "
                         "(factsheet_hash, prompt_version, model); template reports under their own key. "
                         + TRANSPARENCY,
             responses=error_responses(401, 404, 422, 424, 429), dependencies=SIGNED_IN)
def report(body: EngineerReportRequest, request: Request,
           service: EngineerService = Depends(get_engineer_service)) -> EngineerReport:
    return service.report(body, request_id(request))


@router.post("/ask", response_model=EngineerAnswer, summary="Ask the race engineer about a comparison",
             description=f"Follow-up Q&A (≤ {MAX_QUESTION_CHARS} characters after control characters are stripped). "
                         "Out-of-scope questions are refused without an LLM call; the conversation keeps its last "
                         "6 turns server-side (in memory, never persisted). " + TRANSPARENCY,
             responses=error_responses(401, 404, 422, 424, 429), dependencies=SIGNED_IN)
def ask(body: EngineerAskRequest, request: Request,
        service: EngineerService = Depends(get_engineer_service)) -> EngineerAnswer:
    return service.ask(body, request_id(request))


@router.get("/facts/{factsheet_hash}", response_model=FactsResponse, summary="Every fact of a FactSheet (audit)",
            description="All numeric and categorical facts of a FactSheet produced by this server (hover / audit). "
                        "404 when the hash is unknown or has been evicted.",
            responses=error_responses(401, 404, 422), dependencies=SIGNED_IN)
def facts(request: Request, factsheet_hash: str = Path(pattern=FACTSHEET_HASH_PATTERN),
          service: EngineerService = Depends(get_engineer_service)) -> FactsResponse | JSONResponse:
    try:
        return service.facts(factsheet_hash)
    except FactSheetNotFoundError:
        return error_response(request, code="factsheet_not_found", status_code=404,
                              message="unknown FactSheet hash (not built by this server, or evicted)")


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request, x_admin_token: str | None = Header(default=None),
            creds: HTTPAuthorizationCredentials | None = Depends(bearer),
            store: AccountStore = Depends(get_account_store)) -> JSONResponse:
    """Admin only (docs/runbook.md): a signed-in admin, or X-Admin-Token equal to RACEIQ_ADMIN_TOKEN (for
    scrapers without an account; the token path is disabled when RACEIQ_ADMIN_TOKEN is unset)."""
    if creds is not None:
        require_admin(current_user(creds, store))
        return JSONResponse(METRICS.snapshot())
    if not settings.admin_token:
        return error_response(request, code="not_found", status_code=404, message="not found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        return error_response(request, code="forbidden", status_code=403, message="admin token required")
    return JSONResponse(METRICS.snapshot())
