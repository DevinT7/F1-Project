# RaceIQ — AI-Powered Formula 1 Performance Analyzer

Full-stack F1 analytics: telemetry lap comparison, tire-degradation prediction, and an
AI race engineer that explains the results. Data comes from **OpenF1** (live + recent
historical) and **FastF1** (distance-aligned telemetry + older seasons).

> This repo replaces the earlier single-file mockup. The mockup now lives in
> `design/raceiq-dashboard.html` as the visual reference for the frontend.

## Repo layout
```
backend/    FastAPI + OpenF1/FastF1 clients + analysis/tire/AI services   (P1/P2/P3)
frontend/   Vite + React + TS dashboard                                    (P4)
ml/         Tire-degradation training pipeline                             (P3)
design/     raceiq-dashboard.html — visual reference
docs         TEAM_SPLIT.md · DATA_SOURCES.md
```

## Who does what
See **[TEAM_SPLIT.md](TEAM_SPLIT.md)** — 4 parallel owners, one shared contract
(`backend/app/models/schemas.py` ↔ `frontend/src/types.ts`). Where the data comes from:
**[DATA_SOURCES.md](DATA_SOURCES.md)**.

## Run it

### Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; historical data needs no keys
uvicorn app.main:app --reload   # http://localhost:8000/docs
```

OpenF1 setup: no key is required for historical public data; set
`RACEIQ_OPENF1_API_KEY` only for a live/sponsor tier.

FastF1 setup: set `FASTF1_CACHE_DIR` or `RACEIQ_FASTF1_CACHE_DIR` to a persistent
folder and keep `FASTF1_ENABLED=true` for telemetry endpoints. The first request
for a session can be slow while FastF1 warms its local cache.

Redis setup: local development can use the in-memory cache. For Docker or
production, run Redis and set `REDIS_URL=redis://redis:6379/0`.

Try (real data — 2023 Singapore GP race, session_key 9159):
```bash
curl "http://localhost:8000/api/sessions?year=2024"
curl "http://localhost:8000/api/sessions?year=2024&country=Italy"
curl "http://localhost:8000/api/telemetry?session_key=9159&driver_number=1"
curl "http://localhost:8000/api/telemetry/lap?year=2024&grand_prix=Monaco&session=Race&driver=VER&lap=23"
curl "http://localhost:8000/api/telemetry/compare?year=2024&grand_prix=Monaco&session=Race&driver_a=VER&lap_a=23&driver_b=NOR&lap_b=22"
curl "http://localhost:8000/api/analysis/compare?session_key=9159&driver_number=1&reference_driver_number=4"
curl "http://localhost:8000/api/tires/degradation?session_key=9159&driver_number=1"
```

### Frontend (Next.js 16 · Tailwind · Plotly · TanStack Query)
```bash
cd frontend
npm install
BACKEND_URL=http://localhost:8000 npm run dev   # http://localhost:3000 (proxies /api to the backend)
npm run check                                   # typecheck + lint + Vitest (or `make check-frontend`)
npx playwright test                             # e2e smoke (starts the backend on the fixture repo)
```
Share a comparison by URL: `/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:fastest`.
No backend? Click **Demo data** (`?demo=1`): real API output for 2024 China qualifying.
Screenshots and the visual checklist: [docs/screenshots/](docs/screenshots/).

### Ingestion (FastF1 → local Parquet store)
```bash
cd backend
python -m app.ingestion ingest --year 2024 --round 5 --session Q   # ~2.5 min cold, ~1 s re-run
python -m app.ingestion season --year 2024 --sessions Q,R          # every completed round
python -m app.ingestion events --year 2024                         # cached schedule
```
Normalized sessions land in `data/` (gitignored; `RACEIQ_DATA_DIR` overrides). Only
`app/ingestion/fastf1_client.py` imports FastF1; everything else uses the domain models
in `app/ingestion/models.py`. Schemas, the DRS decoding, validity rules and plan
deviations are recorded in [docs/decisions.md](docs/decisions.md).

### Tests (run offline — no network)
```bash
cd backend && pytest -q
pytest -m network                # opt-in live FastF1 integration (~3 min)
mypy                             # strict on app/ingestion + app/storage
```

## API endpoints

### v1 (`/api/v1`, typed, versioned: see `/docs` and `backend/openapi.json`)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/health`, `/api/v1/ready` | liveness / readiness |
| GET | `/api/v1/seasons`, `/api/v1/seasons/{year}/events` | seasons, Grands Prix schedule |
| GET | `/api/v1/sessions/{key}` | session info + availability (`2024-05-Q`) |
| POST | `/api/v1/sessions/{key}/ingest` | ingest via FastF1 (sync) |
| GET | `/api/v1/sessions/{key}/drivers` · `/laps?driver=` · `/fastest-laps` · `/corners` | stored session data |
| POST | `/api/v1/comparisons` | aligned two-lap comparison + delta + findings |

Frontend types: `make gen-types` (or `python -m scripts.export_openapi` in `backend/`, then
`npm run gen-types` in `frontend/`) writes `frontend/src/api/types.ts`.

### Legacy (`/api`)
| Method | Path | Purpose | Owner |
|--------|------|---------|-------|
| GET | `/api/sessions` | list sessions (year/country/type) | P1 |
| GET | `/api/sessions/{key}/drivers` | drivers in a session | P1 |
| GET | `/api/telemetry` | distance-aligned lap trace | P1/P2 |
| GET | `/api/telemetry/lap` | FastF1 chart-ready lap telemetry | P1 |
| GET | `/api/telemetry/compare` | FastF1 aligned lap telemetry + delta | P1 |
| GET | `/api/analysis/compare` | two-lap delta + sectors + corners | P2 |
| GET | `/api/tires/degradation` | degradation + pit window | P3 |
| POST | `/api/ai/report` | grounded natural-language report | P3/P4 |
| GET | `/api/health` | status | P1 |
| GET | `/api/data-status` | provider/cache status | P1 |

## Deployment notes
Redis is required in production so cache state survives process restarts and repeated
telemetry calls do not hammer providers. Mount a persistent FastF1 cache volume
(`FASTF1_CACHE_DIR`) because cold session loads are expensive. Respect provider
limits, and set `FRONTEND_ORIGIN` / `RACEIQ_CORS_ORIGINS` for the deployed frontend.

## Build phases (from the project doc)
1. Telemetry comparison → 2. Corner analysis → 3. Tire model → 4. AI engineer →
5. Production (auth, Redis cache, Docker, CI). Current scaffold covers 1–4 end-to-end
with a baseline tire model; phase 5 and the trained ML model are the next steps.

## Notes
OpenF1/FastF1/Jolpica are unofficial and non-commercial — fine for a portfolio project.
Respect OpenF1 rate limits (3 req/s free); the backend caches aggressively.
