"""Auth dependencies: who is calling, what they may do, how often.

    current_user        401 unless a valid, unrevoked access token is presented (Bearer)
    require_admin       403 unless the user is an admin (the role is read from the database, so
                        a role change takes effect on the next request, not the next token)
    engineer_limit      per-IP token bucket for the LLM-backed endpoints (cost control; they need no
                        account since the frontend dropped sign-in)
    admin_limit         per-user token bucket for ingest / training jobs
"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..config import settings
from ..ratelimit import POLICIES, RateLimited, get_limiter
from .store import AccountStore, MemoryAccountStore, PostgresAccountStore, User
from .tokens import TokenError, decode


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@lru_cache(maxsize=1)
def _account_store() -> AccountStore:
    if settings.database_url:
        from ..db.engine import make_engine
        return PostgresAccountStore(make_engine(settings.database_url))
    return MemoryAccountStore()


def get_account_store() -> AccountStore:
    return _account_store()


bearer = HTTPBearer(auto_error=False, description="access token from POST /api/v1/auth/login")


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                 store: AccountStore = Depends(get_account_store)) -> User:
    if creds is None or creds.scheme.lower() != "bearer":
        raise AuthError(401, "not_authenticated", "sign in to use this endpoint (Authorization: Bearer <token>)")
    try:
        claims = decode(creds.credentials, "access")
    except TokenError as exc:
        raise AuthError(401, "invalid_token", str(exc)) from exc
    user = store.get_user(claims.user_id)
    if user is None or user.token_version != claims.version:
        raise AuthError(401, "token_revoked", "this session has ended; sign in again")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise AuthError(403, "forbidden", "admin role required")
    return user


def _limit(policy: str, user: User) -> User:
    if settings.rate_limit_enabled:
        decision = get_limiter().hit(POLICIES[policy], str(user.id))
        if not decision.allowed:
            raise RateLimited(decision, policy)
    return user


def engineer_limit(request: Request) -> None:
    if settings.rate_limit_enabled:
        from ..ratelimit import client_ip
        decision = get_limiter().hit(POLICIES["engineer"], f"ip:{client_ip(request)}")
        if not decision.allowed:
            raise RateLimited(decision, "engineer")


def admin_limit(user: User = Depends(require_admin)) -> User:
    return _limit("admin", user)


def origin_allowed(request: Request) -> bool:
    """For cookie-authenticated calls: an Origin header, when present, must be an allowed origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    from ..config import allowed_origins
    return origin in allowed_origins()
