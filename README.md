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

Try (real data — 2023 Singapore GP race, session_key 9159):
```bash
curl "http://localhost:8000/api/sessions?year=2024&country=Italy"
curl "http://localhost:8000/api/telemetry?session_key=9159&driver_number=1"
curl "http://localhost:8000/api/analysis/compare?session_key=9159&driver_number=1&reference_driver_number=4"
curl "http://localhost:8000/api/tires/degradation?session_key=9159&driver_number=1"
```

### Frontend
```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173 (proxies /api to :8000)
```
No backend yet? Click **Demo data** in the UI to preview with sample telemetry.

### Tests (run offline — no network)
```bash
cd backend && pytest -q
```

## API endpoints
| Method | Path | Purpose | Owner |
|--------|------|---------|-------|
| GET | `/api/sessions` | list sessions (year/country/type) | P1 |
| GET | `/api/sessions/{key}/drivers` | drivers in a session | P1 |
| GET | `/api/telemetry` | distance-aligned lap trace | P1/P2 |
| GET | `/api/analysis/compare` | two-lap delta + sectors + corners | P2 |
| GET | `/api/tires/degradation` | degradation + pit window | P3 |
| POST | `/api/ai/report` | grounded natural-language report | P3/P4 |
| GET | `/health` | status | P1 |

## Build phases (from the project doc)
1. Telemetry comparison → 2. Corner analysis → 3. Tire model → 4. AI engineer →
5. Production (auth, Redis cache, Docker, CI). Current scaffold covers 1–4 end-to-end
with a baseline tire model; phase 5 and the trained ML model are the next steps.

## Notes
OpenF1/FastF1/Jolpica are unofficial and non-commercial — fine for a portfolio project.
Respect OpenF1 rate limits (3 req/s free); the backend caches aggressively.
