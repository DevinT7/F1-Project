# RaceIQ API reference

Generated from `backend/openapi.json` (API version snapshot) by `python -m scripts.gen_api_docs`; do not edit by hand. Interactive docs: `/docs` on a running API.

Every error uses one envelope: `{"error": {"code", "message", "status_code", "request_id"}}`. Send `X-Request-ID` to correlate logs; the response echoes it. Access: **public**, **signed in** (`Authorization: Bearer <access token>`), or **admin**.

## Meta

Liveness and readiness probes.

### `GET /api/v1/health`

**Liveness probe** · access: public

Always 200 while the process is serving requests. Does not touch storage.

Responses: `200` Successful Response (`HealthResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/ready`

**Readiness probe** · access: public

200 when the session store is readable; 503 otherwise.

Responses: `200` Successful Response (`ReadyResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`); `503` Not ready / tyre model not available (`ErrorResponse`)

## Seasons

Seasons and Grand Prix schedules (FastF1 event schedule, cached per year).

### `GET /api/v1/seasons`

**Available seasons** · access: public

Seasons FastF1 can serve (2018 onward), newest first, with how many sessions of each are already ingested locally.

Responses: `200` Successful Response (`SeasonsResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/seasons/{year}/events`

**Grands Prix of a season** · access: public

Round, name, country, location, format and per-session UTC dates. The schedule is fetched from FastF1 once and cached (past seasons forever, the current one for 24 h).

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `year` | path | yes | `integer` |  |

Responses: `200` Successful Response (`EventsResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Sessions

Ingestion and stored session data: drivers, laps, fastest laps, corners.

### `GET /api/v1/sessions/{session_key}`

**Session info and availability** · access: public

`status` is `ready` (stored, current schema), `not_ingested`, or `stale_schema` (stored by an older version: re-ingest). `info` is present only when ready.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |

Responses: `200` Successful Response (`SessionStatusResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/sessions/{session_key}/corners`

**Circuit corner table** · access: public

Official corners (number, letter, distance along the lap, X/Y in FastF1 decimetres, angle) and the map rotation in degrees.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |

Responses: `200` Successful Response (`CornersResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/sessions/{session_key}/drivers`

**Drivers in a session** · access: public

Code, number, name, team, team colour (`#RRGGBB`) and classification.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |

Responses: `200` Successful Response (`DriversResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/sessions/{session_key}/fastest-laps`

**Fastest valid lap per driver** · access: public

Lowest lap time among laps valid for comparison, per driver, fastest first. Drivers with no valid lap are listed last with `lap: null`.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |

Responses: `200` Successful Response (`FastestLapsResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/sessions/{session_key}/ingest`

**Ingest a session (job)** · access: admin

Queues a background job that downloads (FastF1) and stores the session, and returns it (202, `Location: /api/v1/jobs/{id}`). Poll `GET /api/v1/jobs/{id}` for progress: a cold qualifying session takes ~2.5 min, a race ~7.5 min. Idempotent: an already-stored session finishes at once with `result.skipped: true` unless `force=true`; a second request while one is running returns the running job.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |
| `force` | query | no | `boolean` | re-ingest even if already stored |

Responses: `202` Successful Response (`JobResponse`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `403` Signed in but not allowed (admin role required) or CSRF check failed (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/sessions/{session_key}/laps`

**Laps** · access: public

Every lap with times (s), compound, tyre age, weather at lap start and `is_valid_for_comparison` / `invalid_reason`. Filter with `driver`.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |
| `driver` | query | no | `object` | driver code (VER) or number (1) |

Responses: `200` Successful Response (`LapsResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/sessions/{session_key}/stints`

**Tyre stints per driver** · access: public

Per driver: stint number, compound, first/last lap, tyre age at start, new or used set, total laps, clean laps (after the Phase 6A exclusion rules) and whether the stint is model-eligible (≥ 5 clean laps).

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session_key` | path | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |

Responses: `200` Successful Response (`StintsResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Comparisons

Distance-aligned two-lap comparison with time delta and verified findings. Sign convention: positive delta / time loss = the target lost time.

### `POST /api/v1/comparisons`

**Compare two laps** · access: public

Aligns the target lap to the reference lap by distance (analysis at 1 m), then returns both laps' channels, the cumulative time delta and the Phase 3 verified findings. Every array has `meta.points` entries aligned to `distance_m`; booleans are JSON booleans; missing samples are null. Either lap may be `"fastest"`. Laps not valid for comparison are rejected (422) with the reason.

**Sign convention:** delta_s = target elapsed time − reference elapsed time at the same distance. Positive = the target is behind (has lost time); negative = the target is ahead. delta_s[0] is 0 and delta_s[-1] equals the official lap-time gap (target − reference) within 0.02 s.

Responses are gzip-compressed when the client sends `Accept-Encoding: gzip`, and memoized in-process (`meta.cached`).

Request body: `ComparisonRequest`

Responses: `200` Successful Response (`ComparisonResponse`); `404` Session not ingested / not found (`ErrorResponse`); `413` Request body too large (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `424` Telemetry unavailable for a requested lap (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Tires

Tyre forecasts and a heuristic pit window from the registered production model, using only laps up to the requested lap.

### `GET /api/v1/tires/models`

**Registered tyre models** · access: public

Every registered model version with its status (production | candidate), provenance (dataset hash, git SHA, trained-through date) and 6D evaluation summary.

Responses: `200` Successful Response (`TireModelsResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`); `503` Not ready / tyre model not available (`ErrorResponse`)

### `GET /api/v1/tires/predict`

**Tyre forecast and pit window at a lap** · access: public

Uses only laps ≤ `lap` (all drivers, for the 6A exclusion rules) and the driver's current stint. Needs ≥ 5 clean laps in the stint, otherwise `status: insufficient_data` with a `reason` and no predictions or window. The pit window is a heuristic (interval_crossing_v2): it ignores undercut/overcut, traffic, track position, safety-car probability and tyre availability. Any field that cannot be computed is null and explained in `warnings`.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `session` | query | yes | `string` | Session key "<year>-<round>-<session>", e.g. "2024-05-Q" (2024 round 5 qualifying). Underscores ("2024_05_Q") are accepted too. Session codes: FP1 FP2 FP3 SQ SS S Q R. |
| `driver` | query | yes | `string` | driver code (LEC) or number (16) |
| `lap` | query | yes | `integer` | current lap: the forecast uses laps ≤ lap only |
| `horizon` | query | no | `integer` | laps to forecast (1–25) |
| `threshold` | query | no | `object` | pit-window X in seconds above best pace (default 1.0; a heuristic) |

Responses: `200` Successful Response (`TirePrediction`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`); `503` Not ready / tyre model not available (`ErrorResponse`)

## Auth

Accounts (argon2 passwords), JWT access tokens, the refresh cookie, saved comparisons. Public: comparisons, sessions, tyres. Signed in: the AI engineer. Admin: ingest, training, jobs.

### `POST /api/v1/auth/login`

**Sign in** · access: public

Returns an access token (15 min) and sets the refresh cookie (14 days, httpOnly, Secure, SameSite=Strict, path /api/v1/auth). Unknown email and wrong password give the same 401.

Request body: `Credentials`

Responses: `200` Successful Response (`TokenResponse`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/auth/logout`

**Sign out everywhere** · access: public

Revokes every token of the user (token version bump) and clears the cookie. Needs `X-RaceIQ-CSRF: 1`.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `x-raceiq-csrf` | header | no | `object` |  |
| `raceiq_refresh` | cookie | no | `object` |  |

Responses: `204` Successful Response; `403` Signed in but not allowed (admin role required) or CSRF check failed (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/auth/refresh`

**New access token from the refresh cookie** · access: public

Needs the `raceiq_refresh` cookie and the `X-RaceIQ-CSRF: 1` header. Rotates the refresh cookie; a refresh token is single-use: presenting one twice revokes every session of that user. No cookie at all → 204 (signed out is a normal state, not an error).

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `x-raceiq-csrf` | header | no | `object` |  |
| `raceiq_refresh` | cookie | no | `object` |  |

Responses: `200` Successful Response (`TokenResponse`); `204` no session (no refresh cookie); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `403` Signed in but not allowed (admin role required) or CSRF check failed (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/auth/register`

**Create an account** · access: public

Email + password (≥ 10 characters, stored as an argon2id hash). Returns an access token and sets the refresh cookie. Emails listed in RACEIQ_ADMIN_EMAILS get the admin role.

Request body: `Credentials`

Responses: `201` Successful Response (`TokenResponse`); `409` Conflict (e.g. the email is already registered) (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/me`

**The signed-in user** · access: signed in

Email, role and creation time of the account behind the access token.

Responses: `200` Successful Response (`UserOut`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/me/analyses`

**Saved comparisons** · access: signed in

The signed-in user's saved comparisons, newest first (only their own).

Responses: `200` Successful Response (`SavedAnalysesResponse`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/me/analyses`

**Save a comparison** · access: signed in

Stores the comparison parameters (session, laps, resolution) under a title; re-open it later from the account menu.

Request body: `SaveAnalysisRequest`

Responses: `201` Successful Response (`SavedAnalysisOut`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `DELETE /api/v1/me/analyses/{analysis_id}`

**Delete a saved comparison** · access: signed in

Deletes one of the signed-in user's saved comparisons.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `analysis_id` | path | yes | `string` |  |

Responses: `204` Successful Response; `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/me/analyses/{analysis_id}`

**One saved comparison** · access: signed in

404 when it does not exist or belongs to another user (indistinguishable on purpose).

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `analysis_id` | path | yes | `string` |  |

Responses: `200` Successful Response (`SavedAnalysisOut`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Admin

Admin-only operations (training).

### `POST /api/v1/admin/models/train`

**Retrain the tyre models (job)** · access: admin

Admin only. Rebuilds the servable tyre models and publishes them to the model registry; poll the returned job.

Request body: `TrainRequest`

Responses: `202` Successful Response (`JobResponse`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `403` Signed in but not allowed (admin role required) or CSRF check failed (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Jobs

Background jobs (ingestion, model training): status and progress.

### `GET /api/v1/jobs/{job_id}`

**Background job status** · access: admin

Status and progress of an ingest or training job (poll every 1–2 s until `succeeded` or `failed`). Failed attempts are retried with backoff up to `max_attempts` unless the error is permanent (e.g. the session does not exist).

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `job_id` | path | yes | `string` |  |

Responses: `200` Successful Response (`JobResponse`); `401` Not signed in, or the token is invalid / expired / revoked (WWW-Authenticate: Bearer) (`ErrorResponse`); `403` Signed in but not allowed (admin role required) or CSRF check failed (`ErrorResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Validation Error (`HTTPValidationError`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

## Engineer

AI race engineer: validated reports and follow-up Q&A over a comparison's FactSheet. Numbers come from the analysis engine; the AI only explains them.

### `POST /api/v1/engineer/ask`

**Ask the race engineer about a comparison** · access: public

Follow-up Q&A (≤ 300 characters after control characters are stripped). Out-of-scope questions are refused without an LLM call; the conversation keeps its last 6 turns server-side (in memory, never persisted). Every number is computed by the analysis engine; the AI only phrases the verified facts. `generated_by` says who wrote the text (llm | template | router), `outcome` how it got there (a template is served when the LLM is disabled, unavailable or its text failed validation, with `fallback_reason`), `facts` holds every cited fact and `segments` maps fact prefixes to finding ids. Non-streaming: nothing reaches the client before it passes the validator.

Request body: `EngineerAskRequest`

Responses: `200` Successful Response (`EngineerAnswer`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `424` Telemetry unavailable for a requested lap (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `GET /api/v1/engineer/facts/{factsheet_hash}`

**Every fact of a FactSheet (audit)** · access: public

All numeric and categorical facts of a FactSheet produced by this server (hover / audit). 404 when the hash is unknown or has been evicted.

| Parameter | In | Required | Type | Description |
|---|---|---|---|---|
| `factsheet_hash` | path | yes | `string` |  |

Responses: `200` Successful Response (`FactsResponse`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)

### `POST /api/v1/engineer/report`

**Race engineer report for a comparison** · access: public

Builds the FactSheet for the comparison (plus the target's tyre forecast as of a lap when `tire_context` is given, races only) and returns the validated report. Cached by (factsheet_hash, prompt_version, model); template reports under their own key. Every number is computed by the analysis engine; the AI only phrases the verified facts. `generated_by` says who wrote the text (llm | template | router), `outcome` how it got there (a template is served when the LLM is disabled, unavailable or its text failed validation, with `fallback_reason`), `facts` holds every cited fact and `segments` maps fact prefixes to finding ids. Non-streaming: nothing reaches the client before it passes the validator.

Request body: `EngineerReportRequest`

Responses: `200` Successful Response (`EngineerReport`); `404` Session not ingested / not found (`ErrorResponse`); `422` Invalid request, invalid lap, or comparison failed an integrity check (`ErrorResponse`); `424` Telemetry unavailable for a requested lap (`ErrorResponse`); `429` Rate limited (`ErrorResponse`); `500` Unexpected error (body carries the request id) (`ErrorResponse`)
