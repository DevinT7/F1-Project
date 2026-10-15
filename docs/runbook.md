# Runbook

Operating RaceIQ: deploy, roll back, back up and restore, re-ingest, retrain, rotate keys, and what to do when an alert fires. Setup and platform choices are in [deployment.md](deployment.md); metrics, dashboards and alert definitions in [monitoring.md](monitoring.md).

| Where | Staging | Production |
|---|---|---|
| Backend | Render project `raceiq`, environment `staging`: `raceiq-api-staging`, `raceiq-worker-staging`, `raceiq-db-staging`, `raceiq-kv-staging` | environment `production`: `raceiq-api`, `raceiq-worker`, `raceiq-db`, `raceiq-kv` |
| Frontend | Vercel preview deployment (aliased to `APP_DOMAIN` when set) | Vercel production |
| Deploys | every push to `main`, and every `v*` tag | `v*` tags, after staging, with approval |

## Deploy

- **Release:** `git tag v1.4.0 && git push origin v1.4.0`.
  - The `deploy` workflow runs CI, builds and pushes the images, and deploys staging.
  - The `production` job then waits: GitHub → Actions → the run → **Review deployments** → approve.
- **Staging only:** merge to `main`. It deploys `sha-<12 chars>`.
- **Watch a deploy.** Each environment job logs the image digest, the version wait loop and the smoke test. On Render, service → Events shows the pre-deploy (migrations + seed) output.
- **Done when** the job is green. That means `/api/v1/health` reported the new version, through the frontend proxy too, and `/api/v1/seasons` answered.

**A deploy fails at:**
- **"did not report <version> within 15 minutes".** The pre-deploy command (migrations / seed) failed or the new container is unhealthy. Render keeps serving the previous version. Read Render → `raceiq-api` → Events / Logs, fix forward, then push a new tag.
- **The smoke test.** The backend is on the new version but the frontend (or its proxy) is broken. Roll the frontend back (below) or fix forward.

## Rollback

Redeploy the images of an earlier version. Nothing is rebuilt, and the database is untouched; migrations are expand-only, so the previous release runs on the current schema ([deployment.md](deployment.md), "Migrations are expand-only").

1. **Find the version to return to:** GitHub → Releases / tags, or the last green deploy run (`v1.3.2`, or `sha-<12>` for a main build).
2. **Run the workflow:** GitHub → Actions → **deploy** → **Run workflow**, with `version` = that tag and `environment` = `production` (or `staging`).
   - The job checks that both image tags exist in GHCR and resolves them to digests.
   - Then it does what a normal deploy does: Render api, wait for the version, worker, Vercel built from that tag, smoke test.
   - Production still needs an approval. Any required reviewer can approve within seconds.
3. **Faster, backend only.** Render → service → **Rollback** to an earlier deploy (or the API deploy hook with `&imgURL=ghcr.io/<owner>/raceiq-backend@<digest>`).
4. **Faster, frontend only.** Vercel → Deployments → the previous production deployment → **Promote to Production**.
5. **Afterwards:** a ticket explaining the cause, then fix forward with a new tag.

**Rollback drill (done 2026-10-15)** on the compose stack, with `ops/rollback_drill.sh`:
- **Steps:** deploy `v0.9.1`, register an account, save a comparison; roll back to `v0.9.0` (the previous release's images: an image switch only, the database and volumes stay); roll forward to `v0.9.1`.
- **Result:** each switch took 18–26 s. After each one `/api/v1/health` reported the expected version (`v0.9.1` → `0.1.0`, the pre-Phase-9 build → `v0.9.1`), the account signed in, and the saved comparison was there.
- **Production uses the same mechanics:** an image switch by digest. Repeat the drill before a release that contains a migration:

```bash
PREVIOUS=v0.9.0 CURRENT=v0.9.1 BACKEND_PORT=8000 ops/rollback_drill.sh     # both tags built locally first
```

## Backups

| Layer | What | Retention | Restore |
|---|---|---|---|
| Render managed backups | daily snapshots of `raceiq-db`, point-in-time recovery on paid plans | per plan (≥ 7 days) | Render → `raceiq-db` → Recovery → restore to a **new** database, point the api and worker `RACEIQ_DATABASE_URL` at it, redeploy |
| Logical dump | `ops/db/backup.sh`: pg_dump custom format, AES-256 when `BACKUP_PASSPHRASE` is set (it contains account emails and password hashes) | wherever you keep it | `pg_restore`, or the drill below against the file |
| Not backed up | Redis (cache, rate-limit buckets, job queue: all rebuildable), FastF1 HTTP cache | — | — |

```bash
DATABASE_URL=postgresql://… BACKUP_PASSPHRASE=… ops/db/backup.sh        # → raceiq-<timestamp>.dump.enc
SOURCE_URL=postgresql://…/raceiq ops/db/restore_drill.sh [dump-file]    # restore into a scratch DB and verify
```

**Restore drill (done 2026-10-15)** on the dev database (TimescaleDB, 25 real sessions):
- **Dump:** 828 MB custom-format, taken in 154 s.
- **Restore:** into a scratch database in 91 s, with TimescaleDB pre/post restore.
- **Checks:** the Alembic revision matched; all 15 tables matched row for row (19,849,621 rows: 26,955 laps, 19,817,795 telemetry samples); a lap's telemetry came back contiguous and in order.
- **Total:** 247 s.
- **Also in CI:** the `containers` job repeats the drill on every CI run against the seeded compose database.
- **Production:** repeat it quarterly against a Render restore.

## Re-ingest a session

When: `RaceIQIngestFailed` fired; a session shows `stale_schema`; or the source data was corrected.

1. **Re-ingest.** The web app has no sign-in, so this is an operator task. Either run `python -m app.ingestion ingest --year Y --round R --session C --force` (on Render: the api service → Shell; locally `docker compose exec backend …`), or `POST /api/v1/sessions/{key}/ingest?force=true` with an admin token from `POST /api/v1/auth/login` and poll `GET /api/v1/jobs/{id}`. The UI shows the CLI command for any session that is not stored.
2. **When it fails.** `GET /api/v1/jobs/{id}` shows the error and the attempts.
   - Transient errors (network, FastF1 hiccups) are retried 3 times (5 s, 20 s, 60 s).
   - `PermanentJobError` (the session doesn't exist, or FastF1 couldn't load it) fails at once.
   - The worker logs a `job.failed` line, and Sentry gets a `job-failed/ingest` event.
3. **On success** the session's cached comparisons are invalidated. For a new race, the job result includes the drift score (`"drift": "recorded"`).

**Known upstream problem (2026-10-15).** F1's live-timing server (`livetiming.formula1.com`) now answers **401 Unauthorized** to anonymous requests, from the host and from containers alike. FastF1 3.5.3 then fails with "The data you are trying to access has not been loaded yet", so new ingests fail permanently. Sessions already stored are unaffected. FastF1 3.8.3 is available; upgrading it (and re-checking the ingestion fixtures) is the fix to try ([limitations.md](limitations.md#data-what-raceiq-cannot-know)).

## Retrain the tyre model

When: `RaceIQTyreModelDrift` fired (the rolling MAE on new races is > 25% above the model's baseline), or a season's worth of new races has been ingested.

1. **Rebuild the registry locally.** On a checkout with the data: `cd backend && python -m scripts.build_tire_registry` (6E: split-conformal intervals, checksums, provenance). It rewrites `models/registry.json` and `models/tires/…`.
2. **Gate it.** `python -m scripts.ml_regression` must pass (MAE no worse than the recorded baseline +10%). After a reviewed model change, record a new baseline with `--record`.
3. **Ship it.** Open a PR with the new `models/` (CI runs the gates), merge, and tag. The new image carries the new registry, and the drift baseline gauge follows it automatically.
4. **With object storage configured** (`RACEIQ_MODEL_REGISTRY=db`), `POST /api/v1/admin/models/train` does steps 1 and 3 on the worker instead.

## Rotate keys

All secrets live in Render (service environment) or in GitHub environments; none are in the repository. Rotate one, then redeploy (or restart) the service that reads it.

| Secret | Where | Rotate by | Effect |
|---|---|---|---|
| `RACEIQ_JWT_SECRET` | Render api | Render → Environment → new value → Save (restarts) | operator admin tokens end; log in again (the web app has no accounts) |
| `RACEIQ_METRICS_TOKEN` | Render api + the scraper | new value in Render, then in the scraper config | scrapes fail until both match |
| `LLM_API_KEY` | Render api | create a new key at the provider, update Render, then revoke the old key | none (templates if the key is missing) |
| `SENTRY_DSN` / `SENTRY_DSN_FRONTEND` | Render / GitHub environment variable | Sentry → Client Keys → new key, update, redeploy, disable the old key | none |
| Render deploy hooks | GitHub environment secrets | Render → service → Settings → Deploy Hook → **Regenerate**, update the secret | the old hook stops working |
| `VERCEL_TOKEN` | GitHub environment secret | Vercel → Account → Tokens → create, update, delete the old one | none |
| `ghcr-raceiq` PAT | Render registry credential | new fine-grained PAT (read:packages), update in Render, revoke the old | none |
| Database password | Render | Render → `raceiq-db` → rotate credentials; `fromDatabase` variables update on the next deploy | brief reconnects |
| `BACKUP_PASSPHRASE` | whoever runs backups | new passphrase for new dumps; keep the old one while dumps encrypted with it are retained | — |

A leaked key: rotate it first, then check the provider's usage page, and Sentry and the logs, for misuse. Keys are never logged.

## Alerts

### API down
`RaceIQApiDown`: Prometheus cannot scrape `/metrics`.
- Check Render → `raceiq-api` → Events: a crash loop, or a deploy stuck in pre-deploy.
- `curl https://<api>/api/v1/health` from outside.
- If the last deploy caused it, roll back (above).
- If Postgres or Key Value is down, check the Render status page. `/api/v1/ready` returns 503 when the session store (Postgres) is unreadable; a Key Value outage degrades to uncached responses, and rate limits fail open (logs `cache.redis_unavailable`); queued jobs wait until it is back.

### High error rate
`RaceIQHighErrorRate`: more than 1% of requests are 5xx.
- Grafana "5xx by route" shows where.
- Sentry has the stack traces, tagged `request_id`.
- Logs: the `http.request` lines with a 5xx `status`.
- If a deploy started it, roll back. For a provider outage (FastF1 / OpenF1 `provider_error` 502/503), wait it out: cached data keeps serving.

### Slow responses
`RaceIQSlowComparisons` or `RaceIQCacheHitRatioLow`.
- **Hit ratio low:** Redis evicting (Render → Key Value → memory) or a release changed the cache keys (expected right after a deploy: they warm up).
- **Hit ratio healthy but still slow:** CPU saturation. Scale the instance, or raise `WEB_CONCURRENCY` to the core count.
- **Reference numbers:** the load test ([loadtests/README.md](../loadtests/README.md)) had p95 89 ms at 226 req/s on 4 workers.

### Worker not processing jobs
`RaceIQJobBacklog`.
- Check Render → `raceiq-worker` → Logs for `worker.start`, and restart it.
- Queued jobs survive restarts (RQ in Key Value).
- `raceiq_job_queue_depth{state="failed"}` should stay 0, because the runner records failures in `ingest_jobs`.

### LLM fallback spike
`RaceIQLlmFallbackSpike`, `RaceIQLlmSlow` or `RaceIQLlmCostBudget`.
- Grafana "Violations by rule" and the engineer section below show which rule fails.
- A provider outage shows as `template_provider_error`.
- **Kill switch:** `ENGINEER_LLM_ENABLED=false` on the api, then restart. Users get the validated template ("Template summary").

### Ingest and drift alerts
- `RaceIQIngestFailed` / `RaceIQIngestSlow`: see [Re-ingest a session](#re-ingest-a-session).
- `RaceIQTyreModelDrift`: see [Retrain the tyre model](#retrain-the-tyre-model).

## Local stack (docker-compose.yml)
| Task | Command |
|---|---|
| Start / rebuild | `make up` (or `docker compose up -d --build`); override ports with `FRONTEND_PORT` / `BACKEND_PORT` |
| Stop | `make down` (keeps volumes); `docker compose down -v` wipes the database, S3 and the cache |
| Migrations | `make migrate` (`alembic upgrade head` in the backend container) |
| Re-seed / load all of `data/` | `make seed`; `SEED_FULL_DATA=1 make seed` also loads `./data` (~10 min) |
| Worker logs | `make logs` (backend + worker) |
| Make yourself admin | register the email in `RACEIQ_ADMIN_EMAILS` (default `admin@raceiq.local`) |
| Retrain tyre models | `POST /api/v1/admin/models/train` as an admin, then poll the job |

**Job failures.**

- `GET /api/v1/jobs/{id}` shows the error and the attempts. Transient errors are retried 3 times (5 s / 20 s / 60 s); permanent ones fail at once.
- RQ's own failed registry: `docker compose exec redis redis-cli LRANGE rq:failed:raceiq 0 -1` (it should stay empty; our runner records failures in `ingest_jobs`).

## AI race engineer (Phase 7F)
Endpoints: `POST /api/v1/engineer/report`, `POST /api/v1/engineer/ask`, `GET /api/v1/engineer/facts/{factsheet_hash}`.
Code: `backend/app/api/engineer_service.py`, `backend/app/api/routes/engineer.py`, `backend/app/ai/`.

### Switches

| Setting | Effect |
|---|---|
| `ENGINEER_LLM_ENABLED=false` (or `RACEIQ_ENGINEER_LLM_ENABLED`) | **Kill switch.** Every report and answer is the validated template (`outcome: template_disabled`); the LLM client is never built or called. Takes effect on restart. |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` | The LLM (`docs/prompts.md`). Unset → templates (`template_no_llm`), one `ai.llm_unconfigured` warning. |
| `RACEIQ_ADMIN_TOKEN` | Enables `GET /api/v1/engineer/metrics` (header `X-Admin-Token`). Unset → the endpoint returns 404. |
| `RACEIQ_ENGINEER_FAKE_LLM=inject` | **Test only** (Playwright safety test). A scripted LLM that injects an ungrounded number and a fabricated cause; logs a warning at startup. Never set in production. |
| `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1` | Debug only: logs prompts (FactSheet slices) and question text. Never in production. |
| `NEXT_PUBLIC_FEATURE_ENGINEER=0` | Hides the engineer card in the frontend (build-time). |

### Metrics

Read them with:

```
curl -s -H "X-Admin-Token: $RACEIQ_ADMIN_TOKEN" http://localhost:8000/api/v1/engineer/metrics | jq
```

The counters are per process and reset on restart (`backend/app/ai/metrics.py`). Since Phase 9 the same events are also Prometheus metrics (`raceiq_engineer_requests_total`, `raceiq_llm_*`), summed over workers: [monitoring.md](monitoring.md).

| Key | Meaning |
|---|---|
| `requests_total` | per `report:` / `ask:` and outcome: `llm_first_try`, `llm_after_retry`, `template_validation`, `template_provider_error`, `template_no_llm`, `template_disabled`, `refused`, `clarification` |
| `rates.report_first_try`, `report_after_retry`, `report_fallback` | share of report requests by outcome (cache hits included) |
| `rates.ask_valid_within_retry`, `ask_fallback`, `out_of_scope` | Q&A outcome shares; `out_of_scope` = refused-intent share of all questions |
| `rates.cache_hit_ratio` | report cache hits / lookups |
| `violations_total{rule}` | first-attempt validator violations (what the model gets wrong) |
| `fallback_rules_total{rule}` | rules that forced a template after the retry |
| `llm_latency_ms` | per LLM attempt (count, mean, p50, p95, buckets) |
| `request_latency_ms{kind:path}` | per request; path `llm`, `template` or `cache` |
| `tokens_total`, `cost_usd_total` | cost needs `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` |

**Alert on:**

- `rates.report_fallback` > 0.2 sustained: the prompt or model regressed. Check `fallback_rules_total`.
- `llm_latency_ms.p95` > 8000.
- Any `template_provider_error` burst: the provider is down. Consider the kill switch.

### Logs

There is one structured line per request: `ai.engineer_request`, with `kind`, `factsheet_hash`, `prompt_version`, `outcome`, `cached`, `latency_ms`, `request_id` and `fallback_reason` (`intent` for ask). It never carries the FactSheet or the question text unless the debug flag is set.

**Per LLM attempt:** `ai.report_attempt` / `ai.qa_attempt`, with model, tokens, latency and violated rules. Provider calls log `ai.llm_call` / `ai.llm_call_failed`, with status, request id and transport attempt. API keys never appear.

**Log queries** (JSON logs, e.g. `jq`):

```
# fallback reasons in the last run
jq -r 'select(.event=="ai.engineer_request" and .outcome=="template_validation") | .fallback_reason' app.log | sort | uniq -c
# one user-reported problem: the X-Request-ID from the UI's error chip
jq 'select(.request_id=="<id>")' app.log
# what a FactSheet said (facts are kept in memory, newest 128)
curl -s http://localhost:8000/api/v1/engineer/facts/<factsheet_hash> | jq '.facts[] | select(.id|startswith("T6."))'
```

### Incidents

| Symptom | Action |
|---|---|
| AI text looks wrong | The text passed the validator. Fetch the facts by `factsheet_hash` and file the bullet as a validator corpus case (`backend/tests/ai/corpus/bad.jsonl`). Bump the prompt version if the prompt is changed. |
| Provider outage, slow responses | Set `ENGINEER_LLM_ENABLED=false` and restart. Users get template summaries badged "Template summary". |
| Cost spike | Check `tokens_total` and `cache_hit_ratio`; the kill switch stops all spend. Rate limits are a Phase 8 item (`deps.engineer_rate_limit` is a stub). |
