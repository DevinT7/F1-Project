"""Shared API schema pieces: the error envelope and session-key handling."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SESSION_KEY_PATTERN = r"^\d{4}[-_]\d{2}[-_][A-Za-z0-9]{1,3}$"
_KEY_RE = re.compile(SESSION_KEY_PATTERN)

SESSION_KEY_DESCRIPTION = (
    'Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). '
    'Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R.'
)


def canonical_session_key(value: str) -> str:
    """'2024-05-q' / '2024_05_Q' → '2024_05_Q' (the storage key). Raises ValueError."""
    if not _KEY_RE.match(value):
        raise ValueError(f"invalid session key {value!r}; expected e.g. 2024-05-Q")
    year, rnd, code = re.split(r"[-_]", value)
    return f"{year}_{rnd}_{code.upper()}"


def public_session_key(storage_key: str) -> str:
    return storage_key.replace("_", "-")


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(description="machine-readable error code", examples=["session_not_found"])
    message: str = Field(description="human-readable explanation; never a stack trace")
    status_code: int
    request_id: str | None = Field(default=None, description="echoes the X-Request-ID response header")
    details: Any | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: ErrorDetail


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` entries for the documented error statuses of a route."""
    text = {
        404: "Session not ingested / not found",
        413: "Request body too large",
        422: "Invalid request, invalid lap, or comparison failed an integrity check",
        424: "Telemetry unavailable for a requested lap",
        429: "Rate limited",
        500: "Unexpected error (body carries the request id)",
        503: "Not ready / tyre model not available",
    }
    return {code: {"model": ErrorResponse, "description": text.get(code, "Error")} for code in codes}
