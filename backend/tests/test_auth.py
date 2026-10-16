"""Phase 8 — authentication, roles, saved analyses, rate limits."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import structlog

from app.api import deps
from app.auth import deps as auth_deps
from app.auth.store import MemoryAccountStore, PostgresAccountStore
from app.auth.tokens import issue
from app.config import settings
from app.jobs.queues import InlineQueue
from app.jobs.runner import JobSystem
from app.jobs.store import MemoryJobStore
from app.main import app
from app.ratelimit import POLICIES, MemoryLimiter, Policy, RedisLimiter
from tests import repository_backends as backends

pytestmark = pytest.mark.anyio
PASSWORD = "correct horse battery"
CSRF = {"X-RaceIQ-CSRF": "1"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    js = JobSystem(store=MemoryJobStore(), queue=InlineQueue(defer_retries=True, run=lambda _id: None), handlers={})
    app.dependency_overrides[deps.get_job_system] = lambda: js
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c
    app.dependency_overrides.clear()


async def register(c, email="driver@example.com", password=PASSWORD):
    return await c.post("/api/v1/auth/register", json={"email": email, "password": password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- registration and login
async def test_register_returns_a_token_and_a_secure_refresh_cookie(client):
    r = await register(client, "Driver@Example.com")
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "driver@example.com" and body["user"]["role"] == "user"
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    cookie = r.headers["set-cookie"]
    for attr in ("raceiq_refresh=", "HttpOnly", "Secure", "SameSite=strict", "Path=/api/v1/auth", "Max-Age=1209600"):
        assert attr.lower() in cookie.lower(), attr
    me = await client.get("/api/v1/me", headers=bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == "driver@example.com"


async def test_duplicate_email_and_invalid_input(client):
    assert (await register(client)).status_code == 201
    r = await register(client, "DRIVER@example.com")
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"
    assert (await register(client, "not-an-email")).status_code == 422
    assert (await register(client, "a@b.co", "short")).status_code == 422


async def test_admin_emails_get_the_admin_role(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@example.com, other@example.com")
    assert (await register(client, "Boss@example.com")).json()["user"]["role"] == "admin"


async def test_login_does_not_reveal_which_part_was_wrong(client):
    await register(client)
    wrong = await client.post("/api/v1/auth/login", json={"email": "driver@example.com", "password": "x" * 12})
    unknown = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"] == "wrong email or password"
    ok = await client.post("/api/v1/auth/login", json={"email": "driver@example.com", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["access_token"]


async def test_passwords_are_argon2_hashes_and_emails_never_reach_the_logs(client):
    with structlog.testing.capture_logs() as logs:
        await register(client)
        await client.post("/api/v1/auth/login", json={"email": "driver@example.com", "password": PASSWORD})
        await client.post("/api/v1/auth/login", json={"email": "driver@example.com", "password": "nope-nope-nope"})
    user = auth_deps.get_account_store().get_user_by_email("driver@example.com")
    assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash
    assert logs and all("driver@example.com" not in str(e) and PASSWORD not in str(e) for e in logs)


# ---------------------------------------------------------------- tokens
async def test_expired_tampered_and_wrong_type_tokens_are_401(client):
    body = (await register(client)).json()
    user_id = uuid.UUID(body["user"]["id"])
    expired, _ = issue(user_id, "user", 0, "access", now=datetime.now(timezone.utc) - timedelta(hours=1))
    r = await client.get("/api/v1/me", headers=bearer(expired))
    assert r.status_code == 401 and r.json()["error"]["message"] == "token expired"
    assert r.headers["www-authenticate"].startswith("Bearer")
    tampered = body["access_token"][:-4] + ("AAAA" if not body["access_token"].endswith("AAAA") else "BBBB")
    assert (await client.get("/api/v1/me", headers=bearer(tampered))).status_code == 401
    refresh_as_access, _ = issue(user_id, "user", 0, "refresh")
    assert (await client.get("/api/v1/me", headers=bearer(refresh_as_access))).json()["error"]["code"] == "invalid_token"
    assert (await client.get("/api/v1/me")).json()["error"]["code"] == "not_authenticated"


async def test_refresh_rotates_needs_csrf_and_reuse_revokes_everything(client):
    first = (await register(client)).json()
    old_cookie = client.cookies.get("raceiq_refresh")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 403                      # no CSRF header
    bad_origin = await client.post("/api/v1/auth/refresh", headers={**CSRF, "Origin": "https://evil.example"})
    assert bad_origin.status_code == 403 and bad_origin.json()["error"]["code"] == "csrf_failed"
    r = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert r.status_code == 200 and r.json()["access_token"]
    assert client.cookies.get("raceiq_refresh") != old_cookie                                  # rotated
    # replaying the old (already used) refresh token: theft assumed, every session ends
    client.cookies.clear()
    client.cookies.set("raceiq_refresh", old_cookie)
    replay = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "token_revoked"
    assert (await client.get("/api/v1/me", headers=bearer(first["access_token"]))).status_code == 401
    assert (await client.get("/api/v1/me", headers=bearer(r.json()["access_token"]))).status_code == 401


async def test_refresh_without_a_cookie_is_a_quiet_204(client):
    r = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert r.status_code == 204 and not r.content


async def test_logout_revokes_tokens_and_clears_the_cookie(client):
    body = (await register(client)).json()
    assert (await client.post("/api/v1/auth/logout")).status_code == 403                       # CSRF
    out = await client.post("/api/v1/auth/logout", headers=CSRF)
    assert out.status_code == 204 and 'raceiq_refresh=""' in out.headers["set-cookie"]
    assert (await client.get("/api/v1/me", headers=bearer(body["access_token"]))).json()["error"]["code"] == "token_revoked"


# ---------------------------------------------------------------- roles
async def test_role_enforcement(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "boss@example.com")
    user = (await register(client, "user@example.com")).json()["access_token"]
    admin = (await register(client, "boss@example.com")).json()["access_token"]
    body = {"session_key": "2024-05-Q", "reference": {"driver": "VER"}, "target": {"driver": "ALO"}}

    r = await client.post("/api/v1/sessions/2024-05-Q/ingest")
    assert r.status_code == 401 and "www-authenticate" in r.headers
    r = await client.post("/api/v1/sessions/2024-05-Q/ingest", headers=bearer(user))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    r = await client.post("/api/v1/sessions/2024-05-Q/ingest", headers=bearer(admin))
    assert r.status_code == 202
    job_url = r.json()["status_url"]
    assert (await client.get(job_url, headers=bearer(user))).status_code == 403
    assert (await client.get(job_url, headers=bearer(admin))).status_code == 200
    assert (await client.post("/api/v1/admin/models/train", json={}, headers=bearer(user))).status_code == 403
    assert (await client.post("/api/v1/admin/models/train", json={}, headers=bearer(admin))).status_code == 202

    # the engineer needs no account (the frontend has no sign-in): cost control is the per-IP limit
    assert (await client.post("/api/v1/engineer/report", json=body)).status_code not in (401, 403)  # 200 or 404 by data
    assert (await client.get("/api/v1/engineer/facts/" + "0" * 64)).status_code == 404
    assert (await client.get("/api/v1/engineer/metrics", headers=bearer(user))).status_code == 403
    assert (await client.get("/api/v1/engineer/metrics", headers=bearer(admin))).status_code == 200

    # public: the frictionless demo (comparisons, sessions, tyres) needs no account
    assert (await client.get("/api/v1/seasons")).status_code == 200


async def test_a_role_change_applies_to_existing_tokens(client):
    token = (await register(client)).json()["access_token"]
    store = auth_deps.get_account_store()
    user = store.get_user_by_email("driver@example.com")
    store.update_user(user.id, role="admin")
    assert (await client.get("/api/v1/me", headers=bearer(token))).json()["role"] == "admin"


# ---------------------------------------------------------------- saved analyses
async def test_saved_analyses_crud_and_isolation(client):
    alice = (await register(client, "alice@example.com")).json()["access_token"]
    bob = (await register(client, "bob@example.com")).json()["access_token"]
    params = {"session_key": "2024-05-Q", "reference": {"driver": "VER", "lap": "fastest"},
              "target": {"driver": "ALO", "lap": 14}}
    r = await client.post("/api/v1/me/analyses", json={"title": "Shanghai Q", "params": params}, headers=bearer(alice))
    assert r.status_code == 201
    saved = r.json()
    listed = (await client.get("/api/v1/me/analyses", headers=bearer(alice))).json()["analyses"]
    assert [a["id"] for a in listed] == [saved["id"]] and listed[0]["params"]["target"]["lap"] == 14
    assert (await client.get("/api/v1/me/analyses", headers=bearer(bob))).json()["analyses"] == []
    assert (await client.get(f"/api/v1/me/analyses/{saved['id']}", headers=bearer(bob))).status_code == 404
    assert (await client.delete(f"/api/v1/me/analyses/{saved['id']}", headers=bearer(bob))).status_code == 404
    assert (await client.delete(f"/api/v1/me/analyses/{saved['id']}", headers=bearer(alice))).status_code == 204
    bad = await client.post("/api/v1/me/analyses", json={"title": "x", "params": params | {"session_key": "nope"}},
                            headers=bearer(alice))
    assert bad.status_code == 422
    assert (await client.get("/api/v1/me/analyses")).status_code == 401


# ---------------------------------------------------------------- rate limits
async def test_auth_endpoints_are_rate_limited_per_ip_with_retry_after(client):
    codes = []
    for _ in range(POLICIES["auth"].capacity + 1):
        codes.append((await client.post("/api/v1/auth/login", json={"email": "x@example.com",
                                                                     "password": "x" * 12})).status_code)
    assert codes[:-1] == [401] * POLICIES["auth"].capacity and codes[-1] == 429
    r = await client.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "x" * 12})
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited"


async def test_engineer_is_rate_limited_per_ip_without_an_account(client, monkeypatch):
    monkeypatch.setitem(POLICIES, "engineer", Policy("engineer", 2, 1 / 60))
    body = {"session_key": "2024-99-Q", "reference": {"driver": "VER"}, "target": {"driver": "ALO"}}
    first = [(await client.post("/api/v1/engineer/report", json=body)).status_code for _ in range(2)]
    assert 429 not in first and 401 not in first
    r = await client.post("/api/v1/engineer/report", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    # signing in does not buy a fresh bucket: the key is the client IP
    token = (await register(client)).json()["access_token"]
    assert (await client.post("/api/v1/engineer/report", json=body, headers=bearer(token))).status_code == 429


async def test_the_general_api_limit_applies_per_ip(client, monkeypatch):
    monkeypatch.setitem(POLICIES, "api", Policy("api", 3, 0.5))
    codes = [(await client.get("/api/v1/seasons")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_token_bucket_math():
    now = [0.0]
    limiter = MemoryLimiter(clock=lambda: now[0])
    p = Policy("t", 2, 1.0)
    assert [limiter.hit(p, "a").allowed for _ in range(3)] == [True, True, False]
    assert limiter.hit(p, "a").retry_after_s == 1
    assert limiter.hit(p, "b").allowed                       # per client
    now[0] += 1.0
    assert limiter.hit(p, "a").allowed and not limiter.hit(p, "a").allowed


def test_redis_token_bucket():
    from tests.test_cache import redis_url
    import redis
    client = redis.Redis.from_url(redis_url())
    client.flushdb()
    limiter = RedisLimiter(client)
    p = Policy("t", 3, 0.01)
    results = [limiter.hit(p, "ip-1") for _ in range(4)]
    assert [d.allowed for d in results] == [True, True, True, False]
    assert results[-1].retry_after_s >= 1
    assert limiter.hit(p, "ip-2").allowed
    dead = RedisLimiter(redis.Redis.from_url("redis://127.0.0.1:1/0"))
    assert dead.hit(p, "x").allowed                          # fail open


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    backends.stop()
    from tests import test_cache
    container = test_cache._REDIS.pop("container", None)
    if container is not None:
        container.stop()
    test_cache._REDIS.clear()


# ---------------------------------------------------------------- account stores
@pytest.fixture(params=["memory", "postgres"])
def accounts(request):
    if request.param == "memory":
        return MemoryAccountStore()
    backends.database_url()
    backends.truncate_all()
    return PostgresAccountStore(backends.engine())


def test_account_store_contract(accounts):
    from app.auth.store import EmailTakenError
    u = accounts.create_user("A@Example.com", "$argon2id$x", "user")
    assert u.email == "a@example.com" and accounts.get_user_by_email("a@EXAMPLE.com").id == u.id
    with pytest.raises(EmailTakenError):
        accounts.create_user("a@example.com", "h", "user")
    assert accounts.bump_token_version(u.id).token_version == 1
    assert accounts.update_user(u.id, role="admin").role == "admin"
    a = accounts.add_analysis(u.id, "t", {"session_key": "2024-05-Q"})
    assert [x.id for x in accounts.list_analyses(u.id)] == [a.id]
    other = accounts.create_user("b@example.com", "h", "user")
    assert accounts.get_analysis(other.id, a.id) is None and not accounts.delete_analysis(other.id, a.id)
    assert accounts.delete_analysis(u.id, a.id) and accounts.list_analyses(u.id) == []
