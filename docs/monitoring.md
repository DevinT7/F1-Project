# Monitoring (Phase 9)

How RaceIQ is observed in production: structured logs, Prometheus metrics, a Grafana dashboard, alert rules, and Sentry. The code is in `backend/app/observability.py` and `frontend/src/lib/telemetry.ts`. Incident steps are in [runbook.md](runbook.md).

```
browser ──X-Request-ID──▶ Vercel (Next.js /api proxy) ──▶ api (uvicorn × WEB_CONCURRENCY) ──▶ Postgres / Redis
   │                                                        │  JSON logs (request_id bound)   ▲
   └── Sentry (frontend)                                    │  /metrics ◀── Prometheus ──▶ alert rules
                                                            └── Sentry (backend)            │
                                         worker (RQ) ── job metrics + drift ──▶ Redis ──────┘ (read at scrape)
```

## Logs

- **Format.** `LOG_FORMAT=json` is the container default: one JSON object per line on stdout. Locally, without it, logs are a console format. Standard-library loggers (uvicorn, SQLAlchemy, RQ, FastF1) go through the same renderer.
- **Request id.** Every request gets one. The browser sends `X-Request-ID` (`newRequestId()` in `frontend/src/lib/api/client.ts`); otherwise the API generates it. It is bound into every log line written during the request, echoed in the response header and in the error envelope, shown in the UI's error toast, and tagged on Sentry events. CORS exposes the header.
- **Access log.** One `http.request` line per request, with `method`, `route` (the template, e.g. `/api/v1/sessions/{session_key}`), `status` and `duration_ms`. It has no client IP, query string or raw path. Uvicorn's own access log is off.
- **Redaction.** Keys named `authorization`, `cookie`, `password`, `token`, `access_token`, `refresh_token`, `api_key`, `secret`, `email`, `factsheet` or `question` are replaced by `[redacted]` before rendering (`REDACT_KEYS`). FactSheets and question text are never logged, except at DEBUG with `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1` (never in production).
- **Find a user's problem.** On Render: service → Logs, search `"request_id": "<id from the toast>"`. Locally: `docker compose logs backend worker | grep <id>`.

## Metrics

`GET /metrics` serves the Prometheus text format. It is not in the OpenAPI document.

- **Token.** Set `RACEIQ_METRICS_TOKEN` and scrape with `Authorization: Bearer <token>`. Without a token the endpoint is open in development and returns 404 when `APP_ENV=production`.
- **Several workers.** The image sets `PROMETHEUS_MULTIPROC_DIR`, emptied on every start, so the counters and histograms of all uvicorn workers are summed.
- **Worker metrics.** The RQ worker is never scraped. Job outcomes, durations and drift are written to Redis by whichever process runs the job, and read by the API at scrape time together with the queue depth.

| Metric | Type | Labels | What it answers |
|---|---|---|---|
| `raceiq_http_requests_total` | counter | `method`, `route`, `status` | traffic, error rate (5xx share) |
| `raceiq_http_request_duration_seconds` | histogram | `method`, `route` | latency p50 / p95 / p99 per route |
| `raceiq_cache_lookups_total` | counter | `kind` (`cmp`, `fs`, …), `result` (`hit`, `miss`) | response-cache hit ratio |
| `raceiq_job_queue_depth` | gauge | `state` (`queued`, `scheduled`, `started`, `failed`) | backlog; is the worker alive |
| `raceiq_jobs_total` | counter | `kind` (`ingest`, `train`), `outcome` (`succeeded`, `failed`, `retrying`) | ingest failures |
| `raceiq_job_duration_seconds` | histogram | `kind` | ingest duration |
| `raceiq_engineer_requests_total` | counter | `kind` (`report`, `ask`), `outcome` | LLM first-try / retry / fallback rates |
| `raceiq_llm_latency_seconds` | histogram | — | latency of each LLM attempt |
| `raceiq_llm_tokens_total` | counter | `direction` (`in`, `out`) | token volume |
| `raceiq_llm_cost_usd_total` | counter | — | spend (needs `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK`) |
| `raceiq_llm_violations_total` | counter | `rule` | first-attempt validator violations (violation rate) |
| `raceiq_llm_fallback_rules_total` | counter | `rule` | rules that forced the template |
| `raceiq_tyre_model_rolling_mae_seconds` | gauge | — | **model drift**: the production model's h5 MAE on the last 10 newly ingested races |
| `raceiq_tyre_model_baseline_mae_seconds` | gauge | — | the same model's walk-forward MAE recorded at training time |
| `raceiq_tyre_model_drift_sessions` | gauge | — | races in the drift window |

**Drift** (`backend/app/tires/drift.py`):
- **When it runs.** After each race ingest, the served model replays the new race exactly as the offline evaluation does (forecast cuts every 2 laps from lap 6, horizon 5).
- **What is kept.** That race's MAE joins a window of the last 10 races. The rolling value is weighted by forecast steps.
- **What is skipped.** Races at or before the model's `trained_through_session` are in its training data and are never scored.
- **Measured.** On real data the model scores 0.46 s (2024 R13) and 0.37 s (2024 R14), against a walk-forward baseline of 0.399 s.

**Series exist from the first scrape.** Job counters and the engineer outcome series the alerts divide by are exported at 0 before anything happens. A counter born at 1 shows no `increase()`, so the first ingest failure would otherwise never alert. The end-to-end check caught exactly that.

## Dashboard

`monitoring/grafana/dashboards/raceiq.json` (uid `raceiq-overview`) has 25 panels in four rows:
- **API:** 5xx rate, cached-comparison p95, cache hit ratio, request rate, status classes, latency percentiles, p95 by route, hit ratio by kind, 5xx by route.
- **Jobs:** queue depth, attempts by outcome, ingest p50 / p90.
- **Race engineer:** fallback rate, violations per request, 24 h cost, tokens per minute, outcomes, LLM latency, violations by rule.
- **Tyre model drift:** rolling MAE vs baseline and the alert threshold, races in the window.

It uses a `datasource` variable, so it imports unchanged into Grafana Cloud.

**Locally:**

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d
# Prometheus http://localhost:9090 · Grafana http://localhost:3001 → Dashboards → RaceIQ → RaceIQ
```

**Verified on 2026-10-15** against the compose stack, with traffic covering comparisons, sessions, 404s, engineer reports and Q&A through the fake LLM, and a failing ingest on the real worker:
- All 27 panel queries are valid. 21 returned data.
- The 6 empty ones are series that need events which had not happened: 5xx, job attempts per hour and ingest duration before the first job, and the rolling MAE before the first new race.
- `/metrics` counted 40 of 40 requests spread over 4 uvicorn workers.

## Alerts

`monitoring/prometheus/alerts.yml` has 11 rules. The unit tests in `alerts_test.yml` run in CI (`promtool test rules`). Every alert names its runbook section.

| Alert | Fires when | Severity |
|---|---|---|
| `RaceIQApiDown` | the scrape fails for 5 min | page |
| `RaceIQHighErrorRate` | 5xx > 1% of requests for 10 min (with a traffic floor of 0.1 req/s) | page |
| `RaceIQSlowComparisons` | comparison p95 > 500 ms for 15 min | ticket |
| `RaceIQCacheHitRatioLow` | hit ratio < 50% for 30 min | ticket |
| `RaceIQIngestFailed` | any ingest job failed after its retries in the last hour | ticket |
| `RaceIQIngestSlow` | ingest p90 > 30 min | ticket |
| `RaceIQJobBacklog` | > 10 jobs queued for 15 min | ticket |
| `RaceIQLlmFallbackSpike` | > 20% of LLM-backed engineer requests fell back for 15 min (`template_no_llm`, the no-key case, never counts) | ticket |
| `RaceIQLlmSlow` | LLM p95 > 15 s for 15 min | ticket |
| `RaceIQLlmCostBudget` | > $5 of LLM spend in 24 h | ticket |
| `RaceIQTyreModelDrift` | rolling MAE > 1.25 × baseline, over ≥ 3 races, for 1 h | ticket |

**Verified on 2026-10-15:**
- **Unit tests:** `promtool check rules` reports 11 rules; `promtool test rules` passes, firing and quiet cases alike (for example, 0.5% errors or one 500 at night doesn't page; two races in the drift window don't alert).
- **End to end:** a real ingest failed on the worker. Its outcome went from Redis through `/metrics` to Prometheus, and `RaceIQIngestFailed` was firing within a minute.

**Production.** Point any Prometheus-compatible scraper (for example the Grafana Cloud free tier or Grafana Alloy) at `https://<api>/metrics` with the bearer token, load `alerts.yml`, and route `severity=page` to the on-call channel. That account is not set up yet (docs/api_keys.md → Deployment).

## Sentry

- **When it is on.** Backend: `SENTRY_DSN` on the api and worker. Frontend: `NEXT_PUBLIC_SENTRY_DSN` at build time (GitHub environment variable `SENTRY_DSN_FRONTEND`). Without them nothing is initialised or sent.
- **What is sent.** No PII: `send_default_pii=False`, and the backend's `before_send` drops cookies, request bodies and the user, and redacts auth headers. The frontend's `dataCollection` collects no user info, cookies, bodies or query strings. Events carry `environment`, `release` (the deployed version) and the `request_id` tag.
- **What is reported.** Unhandled API errors (the FastAPI integration). Frontend render crashes and API failures that are network errors or 5xx; 4xx are the UI's business. Two grouped events whose frequency the alerts watch:
  - `"<kind> job failed"`: level error, fingerprint `job-failed/<kind>`, tags `job_kind`, `target`, `error`. Sent once per job that failed after its retries.
  - `"engineer <kind> fell back to the template"`: level warning, fingerprint `llm-fallback/<kind>`. Sent for each validation or provider fallback (never for the no-key template).

Alert rules to create in Sentry (Alerts → Create alert; Sentry stores them, not this repo):

| Rule | Type | Condition | Action |
|---|---|---|---|
| Error rate | Metric alert, `event.type:error`, environment production | > 20 errors in 5 min (or crash-free sessions < 99%) | on-call channel |
| LLM fallback spike | Issue alert on the issue with fingerprint `llm-fallback/*` | > 25 events in 1 h | team channel |
| Ingest failures | Issue alert on `job-failed/ingest` | any new event | team channel |
| New issue | Issue alert | a new issue is created in production | team channel |

The Prometheus alerts above already cover the same signals, so Sentry is the second, independent path (and the one with stack traces).
