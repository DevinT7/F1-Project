# Security checklist (Phase 8)

| Item | How | Where / tested |
|---|---|---|
| **Who may call what** | Public: comparisons, sessions, laps, tyres, seasons (the frictionless demo). Signed in: the AI engineer (LLM cost). Admin: ingest, training, job status, engineer metrics. | `app/auth/deps.py`; `tests/test_auth.py::test_role_enforcement` |
| **Passwords** | argon2id (argon2-cffi defaults, RFC 9106), rehash on login when parameters change, ≥ 10 characters. Unknown email and wrong password give the same 401 and take the same time (a dummy hash is verified). | `app/auth/tokens.py` |
| **Access tokens** | JWT HS256, 15 min, `Authorization: Bearer`; never stored in cookies or `localStorage` (in memory in the browser). | `test_expired_tampered_and_wrong_type_tokens_are_401` |
| **Refresh tokens** | httpOnly + Secure + SameSite=Strict cookie, path `/api/v1/auth`, 14 days. Single-use: rotated on every refresh, and reusing one revokes every session of the user (reuse = theft). | `test_refresh_rotates_needs_csrf_and_reuse_revokes_everything` |
| **Revocation** | `users.token_version` in every token: logout, reuse detection and password changes bump it. Roles are read from the database per request, so demotion is immediate. | `test_logout_…`, `test_a_role_change_applies_to_existing_tokens` |
| **CSRF** | Only `/auth/refresh` and `/auth/logout` use the cookie. They need the custom header `X-RaceIQ-CSRF: 1` (cross-site forms cannot send it; cross-site fetch needs a CORS preflight the allowlist refuses), and an `Origin`, when present, must be allowlisted. Every other endpoint uses Bearer tokens, so it is not CSRF-exposed. | `app/api/routes/auth.py` |
| **CORS** | Allowlist only (`FRONTEND_ORIGIN`, `RACEIQ_CORS_ORIGINS`); the frontend origin must be in it, since the refresh call checks Origin. | `app/config.allowed_origins` |
| **Rate limits** | Redis token buckets, returning 429 + `Retry-After`. Per IP: 120/min for the API, 10/min for register / login / refresh. Per user: 20/min for the engineer, 10/min for admin jobs. Fails open if Redis is down. | `app/ratelimit.py`; `test_*rate_limited*`, `test_redis_token_bucket` |
| **Input validation** | Pydantic models with `extra="forbid"` everywhere; session keys, driver codes, object keys and job ids are pattern-checked; questions are sanitized (control/format characters) and ≤ 300 characters; request bodies ≤ 64 KiB. | `tests/test_engineer_api.py`, `tests/test_api_v1.py` |
| **SQL injection** | SQLAlchemy Core with bound parameters only; telemetry via `COPY` rows. A hostile session key is rejected before any query. | `test_rejects_unsafe_keys` (contract suite) |
| **Secrets** | Environment only (`backend/.env`, git-ignored): `RACEIQ_JWT_SECRET` (required with `APP_ENV=production`, ≥ 32 characters), `RACEIQ_DATABASE_URL`, `AWS_*`, `LLM_API_KEY`. Nothing in `alembic.ini`, compose defaults are dev-only, and keys never appear in logs or errors. | `app/auth/tokens.signing_key`, `docs/api_keys.md` |
| **No PII in logs** | Auth logs carry the user id, never the email or password; questions are not logged (debug flag only). | `test_passwords_are_argon2_hashes_and_emails_never_reach_the_logs` |
| **Errors** | One envelope with a request id; no stack traces to clients; 401 carries `WWW-Authenticate: Bearer`. | `app/api/errors.py` |
| **Dependency audit** | CI runs `pip-audit` on `backend/requirements.txt` and `npm audit --audit-level=high` on the frontend. | `.github/workflows/ci.yml` |
| **Artifacts** | Model artifacts verified against their SHA-256 before serving; object keys cannot escape the bucket or directory. | `tests/test_objectstore_registry.py` |

## Operational notes

- **Bootstrap an admin** by setting `RACEIQ_ADMIN_EMAILS=you@example.com` before registering that address.
- **Rotating `RACEIQ_JWT_SECRET`** signs everyone out (by design).
- **Behind a reverse proxy**, set `RACEIQ_TRUST_PROXY=true` so rate limits use `X-Forwarded-For`. Never set it when the API is exposed directly, because clients could then choose their own IP.
