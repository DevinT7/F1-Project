"""Phase 8 auth and account schemas."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .comparisons import DEFAULT_RESOLUTION_M, LapSelector
from .common import SESSION_KEY_DESCRIPTION, canonical_session_key

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(_Model):
    email: str = Field(max_length=320, examples=["driver@example.com"])
    password: str = Field(min_length=10, max_length=256, description="10–256 characters")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("not a valid email address")
        return value


class UserOut(_Model):
    id: uuid.UUID
    email: str
    role: Literal["user", "admin"]
    created_at: datetime


class TokenResponse(_Model):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="seconds until the access token expires")
    user: UserOut


class AnalysisParams(_Model):
    session_key: str = Field(description=SESSION_KEY_DESCRIPTION)
    reference: LapSelector
    target: LapSelector
    resolution_m: float = DEFAULT_RESOLUTION_M

    @field_validator("session_key")
    @classmethod
    def _key(cls, value: str) -> str:
        canonical_session_key(value)
        return value


class SaveAnalysisRequest(_Model):
    title: str = Field(min_length=1, max_length=200)
    params: AnalysisParams


class SavedAnalysisOut(_Model):
    id: uuid.UUID
    title: str
    params: dict[str, Any]
    created_at: datetime


class SavedAnalysesResponse(_Model):
    analyses: list[SavedAnalysisOut]


class TrainRequest(_Model):
    production: str | None = Field(default=None, description="model kind to mark production (default: the 6E choice)")
