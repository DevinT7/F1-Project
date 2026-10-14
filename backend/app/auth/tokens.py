"""Passwords (argon2id) and tokens (JWT, HS256).

  access token   15 min, sent as `Authorization: Bearer` (never a cookie → no CSRF exposure)
  refresh token  14 days, httpOnly + Secure + SameSite=Strict cookie scoped to /api/v1/auth,
                 rotated on every refresh

Both carry the user's token_version (`ver`): logout and password changes bump it, which
revokes every outstanding token at once. The signing key comes from RACEIQ_JWT_SECRET (required
when APP_ENV=production; a random per-process key otherwise, with a warning).
"""
from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Literal

import jwt
import structlog
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

log = structlog.get_logger("raceiq.auth")

ISSUER = "raceiq"
ALGORITHM = "HS256"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=14)
MIN_PASSWORD = 10

_hasher = PasswordHasher()                  # argon2id, library defaults (RFC 9106 second recommendation)
# a real hash to verify against when the email is unknown, so login time does not reveal accounts
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    return _hasher.check_needs_rehash(stored)


class TokenError(Exception):
    pass


@lru_cache(maxsize=1)
def signing_key() -> str:
    from ..config import settings
    if settings.jwt_secret:
        if len(settings.jwt_secret) < 32:
            raise RuntimeError("RACEIQ_JWT_SECRET must be at least 32 characters")
        return settings.jwt_secret
    if os.environ.get("APP_ENV", "development") == "production":
        raise RuntimeError("RACEIQ_JWT_SECRET is required when APP_ENV=production")
    log.warning("auth.ephemeral_jwt_key", note="RACEIQ_JWT_SECRET unset: tokens are invalid after a restart")
    return secrets.token_urlsafe(48)


@dataclass(frozen=True)
class Claims:
    user_id: uuid.UUID
    role: str
    version: int
    kind: Literal["access", "refresh"]
    jti: str
    expires: datetime


def issue(user_id: uuid.UUID, role: str, version: int, kind: Literal["access", "refresh"],
          now: datetime | None = None) -> tuple[str, datetime]:
    now = now or datetime.now(timezone.utc)
    expires = now + (ACCESS_TTL if kind == "access" else REFRESH_TTL)
    payload: dict[str, Any] = {"iss": ISSUER, "sub": str(user_id), "role": role, "ver": version, "typ": kind,
                               "jti": uuid.uuid4().hex, "iat": now, "exp": expires}
    return jwt.encode(payload, signing_key(), algorithm=ALGORITHM), expires


def decode(token: str, kind: Literal["access", "refresh"]) -> Claims:
    try:
        data = jwt.decode(token, signing_key(), algorithms=[ALGORITHM], issuer=ISSUER,
                          options={"require": ["exp", "iat", "sub", "typ", "ver", "iss"]})
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("invalid token") from exc
    if data.get("typ") != kind:
        raise TokenError(f"not an {kind} token")
    try:
        return Claims(uuid.UUID(data["sub"]), str(data["role"]), int(data["ver"]), kind, str(data.get("jti", "")),
                      datetime.fromtimestamp(int(data["exp"]), timezone.utc))
    except (ValueError, KeyError) as exc:
        raise TokenError("invalid token") from exc
