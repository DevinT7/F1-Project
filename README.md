# RaceIQ: F1 lap analysis with an AI race engineer

RaceIQ compares any two laps of a Formula 1 session distance-aligned. It shows where the time goes (telemetry traces, a time-delta curve, a track map coloured by gain and loss, corner-by-corner findings), forecasts tyre degradation and the pit window for race stints, and lets an AI race engineer explain the comparison. The engineer is held to the computed facts by a validator, with a template fallback.

> **Unofficial project.** RaceIQ is an unofficial, non-commercial portfolio project and is not associated in any way with the Formula 1 companies. F1, FORMULA ONE, FORMULA 1, FIA FORMULA ONE WORLD CHAMPIONSHIP, GRAND PRIX and related marks are trade marks of Formula One Licensing B.V. Timing data comes from the open-source [FastF1](https://github.com/theOehrly/Fast-F1) library; what that data cannot tell you is in [docs/limitations.md](docs/limitations.md).

## Quickstart (Docker, about 10 minutes the first time)

You need Git and Docker with Compose v2 (Docker Desktop, or Docker Engine 24+). Nothing else: no Python, Node or API key.

```bash
git clone https://github.com/DevinT7/F1-Project.git raceiq
cd raceiq
docker compose up -d --build
```

Open **http://localhost:3000** when `docker compose ps` shows `frontend` as healthy. The first build downloads base images and dependencies, which takes several minutes.

What you get:
- **The full stack:** PostgreSQL (TimescaleDB), Redis, S3-compatible storage, the API, a background worker and the web app.
- **Sample data:** 2024 Chinese GP qualifying (VER, PER, ALO) and a synthetic race. The page shows a session picker: choose **2024 → Chinese Grand Prix → Qualifying**, and the two fastest laps (VER vs ALO) are compared. Or open http://localhost:3000/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest.
- **No accounts:** the web app has no sign-in; the AI engineer works for everyone (per-IP rate limit). Sessions are added by the operator from the command line (`docker compose exec backend python -m app.ingestion ingest --year 2024 --round 6 --session Q`); ingest currently fails upstream, see [Known issues](#known-issues).
- **The API docs:** http://localhost:8000/docs.

Ports in use? `FRONTEND_PORT=3300 BACKEND_PORT=8300 docker compose up -d --build`. Stop with `docker compose down` (data is kept); `docker compose down -v` wipes it.

**No Docker at all:** the web app's **Demo data** button (`?demo=1`) serves real API output with no backend.

## Develop

**Backend** (Python 3.11) on the bundled sample store, no services needed:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
RACEIQ_DATA_DIR=tests/fixtures/ingestion/store uvicorn app.main:app --reload --port 8000
```

**Frontend** (Node 22):

```bash
cd frontend
npm ci
BACKEND_URL=http://localhost:8000 npm run dev               # http://localhost:3000, proxies /api to the backend
```

**Checks** (what CI runs; see `.github/workflows/ci.yml`):

```bash
cd backend && ruff check app scripts tests && python -m mypy && python -m pytest -q -m "not network and not perf"
cd frontend && npm run check                                # typecheck + lint + Vitest
cd frontend && npx playwright test                          # end-to-end (starts its own backend + frontend)
```

**Other entry points:**
- **More data:** `python -m app.ingestion backfill --from-year 2018 --to-year 2026` ingests completed FP1/FP2/FP3/Q/R sessions into `data/` (Parquet). Use `season --year 2024` for one year or `--sessions Q,R` for a smaller pull. `SEED_FULL_DATA=1 docker compose run --rm seed` loads `data/` into the stack.
- **API types:** `make gen-types` regenerates `frontend/src/api/types.ts` from the reviewed `backend/openapi.json`. CI fails when the API changes without it.

## How it is built

| Part | Stack |
|---|---|
| API | FastAPI, SQLAlchemy Core + Alembic, PostgreSQL / TimescaleDB, Redis (cache, RQ jobs, rate limits) |
| Analysis | distance alignment, time delta, corners and findings (`backend/app/processing/`) |
| Tyre model | pooled fixed-effects degradation with conformal intervals, a checksummed registry, drift monitoring |
| AI engineer | FactSheet → LLM (optional) → validator → retry → template fallback |
| Web app | Next.js 16, TanStack Query, Plotly, Tailwind |
| Delivery | multi-stage non-root images, GitHub Actions (tests, coverage and mutation gates, Trivy, compose e2e), Render + Vercel, Prometheus / Grafana / Sentry |

## Docs

| | |
|---|---|
| [architecture.md](docs/architecture.md) | components, request and data paths, code map (diagram) |
| [api.md](docs/api.md) | every endpoint: access, parameters, responses |
| [deployment.md](docs/deployment.md) | Render + Vercel, environments, the delivery pipeline, first deploy |
| [runbook.md](docs/runbook.md) | deploy, rollback, backups, re-ingest, retrain, key rotation, alert responses |
| [monitoring.md](docs/monitoring.md) | logs, metrics, dashboard, alerts, Sentry |
| [limitations.md](docs/limitations.md) | what the data cannot tell you; data policies; the unofficial notice |
| [security.md](docs/security.md) | auth, cookies, rate limits |
| [api_keys.md](docs/api_keys.md) | which features need which key (none to run) |
| [decisions.md](docs/decisions.md), [progress.md](docs/progress.md) | design decisions and phase results |
| [TEAM_SPLIT.md](TEAM_SPLIT.md), [DATA_SOURCES.md](DATA_SOURCES.md) | who owns what; where the data comes from |

## Known issues

- **New ingests fail upstream (since 2026-10-15).** F1's live-timing server now rejects anonymous requests (HTTP 401), so FastF1 3.5.3 cannot load new sessions. Stored and sample sessions work normally. The fix to try is upgrading FastF1 (3.8.3 is out); see the [runbook](docs/runbook.md#re-ingest-a-session).
- **The live LLM evaluation is pending an API key.** Without a key the engineer serves validated template reports ([api_keys.md](docs/api_keys.md)).

**GitHub Pages preview:** the Pages workflow publishes a static, fixture-backed demo (2024 Chinese GP qualifying). It does not host the FastAPI backend, ingest data, or enable live AI calls. For a full deployment, use the existing Render + Vercel pipeline in [docs/deployment.md](docs/deployment.md).
