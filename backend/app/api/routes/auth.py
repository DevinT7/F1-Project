"""Phase 8 accounts: register, login, refresh (cookie), logout, /me, saved analyses, admin jobs."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from ...auth.deps import AuthError, admin_limit, current_user, get_account_store, origin_allowed
from ...auth.store import AccountStore, EmailTakenError, User
from ...auth.tokens import ACCESS_TTL, REFRESH_TTL, TokenError, decode, hash_password, issue, needs_rehash, verify_password
from ...caching import cache_key, get_cache
from ...config import settings
from ...jobs.runner import JobSystem
from ..deps import get_job_system
from ..errors import error_response
from ..schemas.auth import (
    Credentials, SaveAnalysisRequest, SavedAnalysesResponse, SavedAnalysisOut, TokenResponse, TrainRequest, UserOut,
)
from ..schemas.common import error_responses
from ..schemas.jobs import JobResponse

log = structlog.get_logger("raceiq.auth")

REFRESH_COOKIE = "raceiq_refresh"
COOKIE_PATH = "/api/v1/auth"
CSRF_HEADER = "X-RaceIQ-CSRF"

auth = APIRouter(prefix="/auth", tags=["v1: auth"])
me = APIRouter(prefix="/me", tags=["v1: auth"])
admin = APIRouter(prefix="/admin", tags=["v1: admin"])


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role, created_at=user.created_at)  # type: ignore[arg-type]


def _session(user: User, response: Response) -> TokenResponse:
    """Access token in the body; a fresh refresh token in the httpOnly cookie."""
    access, _ = issue(user.id, user.role, user.token_version, "access")
    refresh, _ = issue(user.id, user.role, user.token_version, "refresh")
    response.set_cookie(REFRESH_COOKIE, refresh, max_age=int(REFRESH_TTL.total_seconds()), path=COOKIE_PATH,
                        httponly=True, secure=True, samesite="strict")
    return TokenResponse(access_token=access, expires_in=int(ACCESS_TTL.total_seconds()), user=_user_out(user))


def _require_csrf(request: Request, csrf: str | None) -> None:
    """Cookie-authenticated endpoints: a custom header (cannot be sent cross-site without a CORS
    preflight, which the allowlist refuses) plus, when sent, an allowed Origin."""
    if csrf != "1" or not origin_allowed(request):
        raise AuthError(403, "csrf_failed", f"missing {CSRF_HEADER} header or disallowed origin")


@auth.post("/register", response_model=TokenResponse, status_code=201, summary="Create an account",
           description="Email + password (≥ 10 characters, stored as an argon2id hash). Returns an access token "
                       "and sets the refresh cookie. Emails listed in RACEIQ_ADMIN_EMAILS get the admin role.",
           responses=error_responses(409, 422, 429))
def register(body: Credentials, response: Response, store: AccountStore = Depends(get_account_store)) -> TokenResponse:
    role = "admin" if body.email in settings.admin_email_set else "user"
    try:
        user = store.create_user(body.email, hash_password(body.password), role)
    except EmailTakenError:
        raise AuthError(409, "email_taken", "an account with this email already exists") from None
    log.info("auth.registered", user_id=str(user.id), role=role)        # never the email (PII)
    return _session(user, response)


@auth.post("/login", response_model=TokenResponse, summary="Sign in",
           description="Returns an access token (15 min) and sets the refresh cookie (14 days, httpOnly, Secure, "
                       "SameSite=Strict, path /api/v1/auth). Unknown email and wrong password give the same 401.",
           responses=error_responses(401, 422, 429))
def login(body: Credentials, response: Response, store: AccountStore = Depends(get_account_store)) -> TokenResponse:
    user = store.get_user_by_email(body.email)
    if not verify_password(user.password_hash if user else None, body.password) or user is None:
        log.info("auth.login_failed")
        raise AuthError(401, "invalid_credentials", "wrong email or password")
    if needs_rehash(user.password_hash):
        user = store.update_user(user.id, password_hash=hash_password(body.password))
    log.info("auth.login", user_id=str(user.id))
    return _session(user, response)


@auth.post("/refresh", response_model=TokenResponse, summary="New access token from the refresh cookie",
           description=f"Needs the `{REFRESH_COOKIE}` cookie and the `{CSRF_HEADER}: 1` header. Rotates the refresh "
                       "cookie; a refresh token is single-use: presenting one twice revokes every session of that user. "
                       "No cookie at all → 204 (signed out is a normal state, not an error).",
           responses={**error_responses(401, 403, 429), 204: {"description": "no session (no refresh cookie)"}})
def refresh(request: Request, response: Response, store: AccountStore = Depends(get_account_store),
            raceiq_refresh: str | None = Cookie(default=None),
            x_raceiq_csrf: str | None = Header(default=None)) -> TokenResponse | Response:
    _require_csrf(request, x_raceiq_csrf)
    if not raceiq_refresh:
        return Response(status_code=204)
    try:
        claims = decode(raceiq_refresh, "refresh")
    except TokenError as exc:
        raise AuthError(401, "invalid_token", str(exc)) from exc
    user = store.get_user(claims.user_id)
    if user is None or user.token_version != claims.version:
        raise AuthError(401, "token_revoked", "this session has ended; sign in again")
    ttl = max(1, int((claims.expires - datetime.now(timezone.utc)).total_seconds()))
    if not get_cache().add(cache_key("rt", "used", claims.jti), b"1", ttl):
        store.bump_token_version(user.id)                     # reuse of a rotated token: assume theft
        log.warning("auth.refresh_reuse", user_id=str(user.id))
        raise AuthError(401, "token_revoked", "refresh token reused; every session was signed out")
    return _session(user, response)


@auth.post("/logout", status_code=204, summary="Sign out everywhere",
           description=f"Revokes every token of the user (token version bump) and clears the cookie. Needs `{CSRF_HEADER}: 1`.",
           responses=error_responses(403))
def logout(request: Request, store: AccountStore = Depends(get_account_store),
           raceiq_refresh: str | None = Cookie(default=None),
           x_raceiq_csrf: str | None = Header(default=None)) -> Response:
    _require_csrf(request, x_raceiq_csrf)
    if raceiq_refresh:
        try:
            claims = decode(raceiq_refresh, "refresh")
            user = store.get_user(claims.user_id)
            if user is not None and user.token_version == claims.version:
                store.bump_token_version(user.id)
        except TokenError:
            pass
    out = Response(status_code=204)
    out.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    return out


# ---------------------------------------------------------------- the signed-in user
@me.get("", response_model=UserOut, summary="The signed-in user",
        description="Email, role and creation time of the account behind the access token.",
        responses=error_responses(401))
def whoami(user: User = Depends(current_user)) -> UserOut:
    return _user_out(user)


@me.get("/analyses", response_model=SavedAnalysesResponse, summary="Saved comparisons",
        description="The signed-in user's saved comparisons, newest first (only their own).",
        responses=error_responses(401))
def list_analyses(user: User = Depends(current_user),
                  store: AccountStore = Depends(get_account_store)) -> SavedAnalysesResponse:
    return SavedAnalysesResponse(analyses=[SavedAnalysisOut(id=a.id, title=a.title, params=a.params,
                                                            created_at=a.created_at)
                                           for a in store.list_analyses(user.id)])


@me.post("/analyses", response_model=SavedAnalysisOut, status_code=201, summary="Save a comparison",
         description="Stores the comparison parameters (session, laps, resolution) under a title; re-open it "
                     "later from the account menu.",
         responses=error_responses(401, 422))
def save_analysis(body: SaveAnalysisRequest, user: User = Depends(current_user),
                  store: AccountStore = Depends(get_account_store)) -> SavedAnalysisOut:
    a = store.add_analysis(user.id, body.title, body.params.model_dump(mode="json"))
    return SavedAnalysisOut(id=a.id, title=a.title, params=a.params, created_at=a.created_at)


@me.get("/analyses/{analysis_id}", response_model=SavedAnalysisOut, summary="One saved comparison",
        description="404 when it does not exist or belongs to another user (indistinguishable on purpose).",
        responses=error_responses(401, 404))
def get_analysis(analysis_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                 store: AccountStore = Depends(get_account_store)) -> SavedAnalysisOut | JSONResponse:
    a = store.get_analysis(user.id, analysis_id)                # another user's id is simply not found
    if a is None:
        return error_response(request, code="analysis_not_found", status_code=404, message="no such saved analysis")
    return SavedAnalysisOut(id=a.id, title=a.title, params=a.params, created_at=a.created_at)


@me.delete("/analyses/{analysis_id}", status_code=204, summary="Delete a saved comparison",
           description="Deletes one of the signed-in user's saved comparisons.",
           responses=error_responses(401, 404))
def delete_analysis(analysis_id: uuid.UUID, request: Request, user: User = Depends(current_user),
                    store: AccountStore = Depends(get_account_store)) -> Response:
    if not store.delete_analysis(user.id, analysis_id):
        return error_response(request, code="analysis_not_found", status_code=404, message="no such saved analysis")
    return Response(status_code=204)


# ---------------------------------------------------------------- admin
@admin.post("/models/train", response_model=JobResponse, status_code=202, summary="Retrain the tyre models (job)",
            description="Admin only. Rebuilds the servable tyre models and publishes them to the model registry; "
                        "poll the returned job.", responses=error_responses(401, 403, 429))
def train(body: TrainRequest, response: Response, user: User = Depends(admin_limit),
          jobs: JobSystem = Depends(get_job_system)) -> JobResponse:
    job = jobs.submit("train", "tire-models", body.model_dump(exclude_none=True), max_attempts=1, created_by=user.id)
    job = jobs.store.get(job.id) or job
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return JobResponse.of(job)
