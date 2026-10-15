# API keys: which phase needs what

Keys go in `backend/.env` (git-ignored; template: `backend/.env.example`). **No key is needed to run the app or the test suite.** Every AI path falls back to validated template output, and CI never calls a provider.

Status as of 2026-10-15: **no key or deployment credential is configured.**

| Phase | Needs a key? | Which | What is blocked without it | Status |
|---|---|---|---|---|
| 1 Ingestion (FastF1, OpenF1 historical) | No | `RACEIQ_OPENF1_API_KEY` only for the OpenF1 live/sponsor tier | nothing used by the project | done |
| 2 Alignment, 3 Insights, 4 API | No | — | — | done |
| 5 Frontend dashboard | Optional | `RACEIQ_OPENAI_API_KEY` | `POST /api/ai/report` serves its template report instead of LLM prose | done (template path) |
| 6 Tyre model and panel | No | — | — | done |
| 7A FactSheet, 7B Validator, 7C Template report | No | — | — | done |
| **7D LLM report generation** | **Yes, for the gate** | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` | live eval (≥ 30 reports: ≥ 95% valid within one retry, ≥ 80% first try), cost/latency numbers, human grading of 10 reports | ⏳ **placeholder: API key needed** |
| **7E Follow-up Q&A** | **Yes, for the gate** | same `LLM_*` | live check (40 questions / 8 comparisons, ≥ 95% valid within one retry), review of ten LLM transcripts | ⏳ **placeholder: API key needed** |
| 7F API, caching, frontend, observability | No for the build, the E2E safety test, kill switch, cache and metrics (fake LLMs + templates); **yes for the final live evaluation** | same `LLM_*` | final live evaluation (first-try / post-retry / fallback rates, cost per report), LLM-path p95 latency < 8 s, the LLM run of the demo script | code done; ⏳ **placeholder: API key needed** for the live items |
| 8 Persistence, cache, jobs, auth, object storage | **No** (local services: Postgres, Redis, SeaweedFS; credentials are dev defaults or your own `.env`) | — | — | done |
| **9 Quality, CI/CD, deployment, monitoring** | **No** for everything that runs locally or in CI (gates, images, Trivy, compose e2e, load test, rollback and restore drills, metrics, dashboard, alert tests). **Yes, account credentials, for the hosted deploy and for error reporting** | see *Deployment credentials* below | the deploy workflow skips staging (warning on main, error on tags); Sentry stays off; nobody scrapes `/metrics` in production | code done and verified locally; ⏳ **placeholder: credentials needed** for the hosted environments |
| 10 and later | to be assessed when each phase spec arrives | | | |

## Where the key is needed (placeholders)

| Place | What it does without a key |
|---|---|
| `backend/.env.example` → `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | commented-out placeholders marked "API KEY NEEDED" |
| `python -m scripts.eval_report_quality --live` (7D) | prints **"API KEY NEEDED"** with the variables to set, exit code 3 |
| `python -m scripts.eval_qa_quality --live` (7E) | same message, exit code 3 |
| `app.ai.llm_client.client_from_env()` (used by the 7F endpoints) | returns `None` and logs `ai.llm_unconfigured` once; reports and answers come from the templates (`template_no_llm`) |
| `docs/progress.md` → "7D live evaluation" / "7E live Q&A check" | placeholder sections, replaced by the eval scripts' results |
| `docs/progress.md` → "7F final live evaluation" | placeholder; filled from the 7D script's output (it reports p95 latency too) |
| `/api/v1/engineer/*` with no key | serve validated templates (`outcome: template_no_llm`), badged "Template summary" in the UI |

## When a key is added

```
cd backend
python -m scripts.eval_report_quality --live      # 7D gate
python -m scripts.eval_qa_quality --live          # 7E gate
```

Both scripts append their results to `docs/progress.md`, under the placeholders. Then grade the sampled reports and transcripts. The free providers (Groq, OpenRouter `:free` models, local Ollama) work through `LLM_PROVIDER=openai` plus `LLM_BASE_URL`; see `docs/prompts.md`.

## Deployment credentials (Phase 9)

None of these is needed to run, test or build RaceIQ. They connect the delivery pipeline to the hosting accounts. Values go in the GitHub environments `staging` and `production` (Settings → Environments) or in Render; never in the repository. Setup order: [deployment.md](deployment.md) → First deploy.

| Name | Kind | Where it goes | Needed for | Without it |
|---|---|---|---|---|
| `RENDER_DEPLOY_HOOK_API`, `RENDER_DEPLOY_HOOK_WORKER` | secret | GitHub environment | deploying the backend images | **API KEY NEEDED**: `deploy-env.yml` preflight lists the missing names; main pushes skip the deploy with a warning, tag pushes fail |
| `VERCEL_TOKEN`, `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | secret | GitHub environment | deploying the frontend | same preflight message |
| `API_URL` | variable | GitHub environment | the frontend's `/api` proxy target + the deploy's version check | same preflight message |
| `ghcr-raceiq` registry credential (GitHub PAT, read:packages) | Render setting | Render → Registry credentials | Render pulling private GHCR images | Render cannot pull (not needed if the packages are public) |
| `SENTRY_DSN` | secret | Render (api, worker) | backend error reporting | Sentry is not initialised (`init_sentry` returns False); nothing is sent |
| `SENTRY_DSN_FRONTEND` (+ optional `SENTRY_ORG`, `SENTRY_PROJECT`, `SENTRY_AUTH_TOKEN`) | variable / secret | GitHub environment | frontend error reporting (+ source-map upload) | the browser SDK is not initialised; source maps are not uploaded |
| `RACEIQ_METRICS_TOKEN` | secret | generated by Render (`render.yaml`); copy to the scraper | scraping `/metrics` in production | `/metrics` returns 404 in production |
| Grafana Cloud (or any Prometheus) account | account | the scraper's config | the dashboard + alerts in production | the dashboard and rules exist and are tested (`monitoring/`), but nothing evaluates them in production |
| `VERCEL_AUTOMATION_BYPASS_SECRET` | secret, optional | GitHub environment | smoke-testing protected preview deployments | the staging smoke test gets 401 if Vercel deployment protection is on |
| `BACKUP_PASSPHRASE` | secret, optional | wherever `ops/db/backup.sh` runs | encrypting logical dumps | dumps are written unencrypted (they contain account emails and password hashes) |

**Placeholders in the repository:**
- **`render.yaml`:** `sync: false` entries, marked "API KEY NEEDED", that Render asks for when the Blueprint is applied.
- **`.github/workflows/deploy-env.yml`:** the preflight step's "API KEY NEEDED" message.
- **`backend/.env.example`:** the Phase 9 block.
- **`docs/progress.md`:** "Phase 9: hosted environments", pending credentials.
