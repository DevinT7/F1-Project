"""Shared test setup: every test starts with fresh rate-limit buckets and a fresh in-memory
account store (so one test's requests or accounts never leak into another)."""
from __future__ import annotations

import uuid

import pytest

from app.auth import deps as auth_deps
from app.auth.store import User
from app.ratelimit import get_limiter


@pytest.fixture(autouse=True)
def _fresh_limits_and_accounts(monkeypatch):
    # The app under test never inherits infrastructure from the environment (a CI job or a developer
    # shell may set RACEIQ_DATABASE_URL / RACEIQ_REDIS_URL): accounts, jobs, cache and limits are in
    # memory unless a test wires a backend explicitly (RACEIQ_TEST_DATABASE_URL / _REDIS_URL).
    from app.config import settings
    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setattr(settings, "redis_url", None)
    get_limiter.cache_clear()
    auth_deps._account_store.cache_clear()
    yield
    get_limiter.cache_clear()
    auth_deps._account_store.cache_clear()


def make_user(role: str = "user") -> User:
    return User(id=uuid.uuid4(), email=f"{role}-{uuid.uuid4().hex[:6]}@example.com", password_hash="x", role=role)


@pytest.fixture
def as_user():
    """Override current_user: the API sees a signed-in user (role 'user')."""
    from app.main import app
    user = make_user("user")
    app.dependency_overrides[auth_deps.current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(auth_deps.current_user, None)


@pytest.fixture
def as_admin():
    from app.main import app
    user = make_user("admin")
    app.dependency_overrides[auth_deps.current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(auth_deps.current_user, None)
