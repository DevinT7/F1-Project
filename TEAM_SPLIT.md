# RaceIQ — Team Split (4 people)

The repo is a **monorepo** deliberately split so each person owns a directory and a
clear interface. You can all work in parallel because everyone codes against the
**shared contract** in `backend/app/models/schemas.py` (and its mirror in
`frontend/src/types.ts`). Agree on that contract first; then nobody blocks anyone.

```
F1-Project/
├── backend/          → Person 1 (owns) + shared
│   └── app/
│       ├── clients/      P1  data sources (OpenF1, FastF1)
│       ├── routers/      P1  HTTP endpoints
│       ├── services/
│       │   ├── analysis.py   P2  lap align / delta / corners
│       │   ├── telemetry.py  P2  telemetry shaping
│       │   ├── tires.py      P3  degradation model
│       │   └── ai_engineer.py P3 → P4 narrative layer
│       └── models/schemas.py  SHARED CONTRACT (edit together)
├── ml/               → Person 3  training + datasets
├── frontend/         → Person 4  React dashboard
└── docs/             → everyone
```

## The contract (do this together, day 1)
`backend/app/models/schemas.py` defines every response shape. Frontend mirrors it in
`frontend/src/types.ts`. Change it via PR so all four stay in sync. As long as the
shapes are stable, each person can build and test in isolation with mock data.

---

## Person 1 — Data & Backend Platform
**Owns:** `backend/app/clients/`, `backend/app/routers/`, `backend/app/main.py`,
`backend/app/cache.py`, `docker-compose.yml`, deployment.

- Wrap OpenF1 REST (`clients/openf1.py`) and FastF1 (`clients/fastf1_client.py`).
- Expose clean endpoints: sessions, drivers, laps, telemetry, stints, weather.
- Caching (Redis in prod, in-memory for dev) + rate-limit friendliness (OpenF1 free
  tier = 3 req/s, 30 req/min).
- Health checks, CORS, error handling, `/docs` (auto Swagger).

**Definition of done:** `GET /api/sessions?year=2024` returns real data; other teams
can call every endpoint locally. **Depends on:** nobody. **Unblocks:** everyone.

## Person 2 — Analysis Engine
**Owns:** `backend/app/services/analysis.py`, `backend/app/services/telemetry.py`,
and the analysis router logic.

- Align two laps by **distance** (interpolate — OpenF1 samples aren't distance-indexed).
- Compute time-delta trace, sector deltas, and per-corner metrics
  (entry/min/exit speed, braking point, throttle delay).
- Corner detection (from FastF1 circuit info or speed-minima heuristics).
- Output the structured `LapComparison` / `CornerInsight` objects in the contract.

**Definition of done:** `GET /api/analysis/compare` returns real deltas for two
drivers. **Depends on:** P1's telemetry endpoint shape (mock it until ready).
**Unblocks:** P3 (tire features) and P4 (charts).

> Decided (P2, Phase 0): `GET`, not `POST`. The endpoint only reads/derives data from
> query params (session_key, driver_number, reference_driver_number, lap_number,
> reference_lap_number, include_traces) with no side effects, so it's naturally
> cacheable, bookmarkable, and matches what's already implemented in
> `routers/analysis.py`. Revisit only if the param list grows enough to need a body.

## Person 3 — ML: Tire Degradation + AI Layer
**Owns:** `ml/`, `backend/app/services/tires.py`, `backend/app/services/ai_engineer.py`.

- `ml/build_dataset.py`: assemble stint training data (remove pit/SC/invalid laps,
  fuel-correct lap times).
- `ml/train.py`: train + compare LinearReg / RandomForest / XGBoost / LightGBM;
  log MAE/RMSE; save model artifact.
- `tires.py`: load model, predict degradation, pit window, confidence band. Ships a
  **baseline linear-fit** so the endpoint works before the ML model lands.
- `ai_engineer.py`: turn structured findings into a narrative. **Guardrail:** the LLM
  only rephrases numbers computed by P2/P3 — it never invents stats. Works with a
  template fallback when no API key is set.

**Definition of done:** `GET /api/tires/degradation` and `POST /api/ai/report`
return real output. **Depends on:** P2's structured findings. **Unblocks:** P4.

## Person 4 — Frontend Dashboard
**Owns:** `frontend/` entirely.

- Vite + React + TypeScript. Reuse the dark "pit wall" design in
  `design/raceiq-dashboard.html` as the visual language.
- Build the API client (`src/api.ts`) + types mirror, and wire the real telemetry
  charts (Chart.js/Recharts), interactive track map, tire panel, AI report.
- Loading / empty / error states; session & driver pickers hit P1's endpoints.

**Definition of done:** dashboard renders live data end-to-end from the backend.
**Depends on:** the contract (not the implementation — mock `api.ts` until backend is
up). **Unblocks:** demo.

---

## Parallelization rules
1. **Contract first.** Freeze `schemas.py` shapes before splitting off.
2. **Mock at the boundary.** P4 mocks `api.ts`; P2/P3 mock P1's client with saved JSON
   fixtures in `backend/tests/fixtures/`. Nobody waits.
3. **One endpoint, one owner.** Routers are thin; logic lives in each owner's service.
4. **Small PRs** against `main`; the contract file needs a second reviewer.

## Suggested 5-phase timeline (maps to the project doc)
| Phase | Lead | Deliverable |
|-------|------|-------------|
| 1 Telemetry comparison | P1+P2 | fetch + align + delta charts |
| 2 Corner analysis | P2 | track map + corner insights |
| 3 Tire model | P3 | trained model + predictions |
| 4 AI race engineer | P3+P4 | narrative report + follow-ups |
| 5 Production | P1 | auth, caching, Docker, CI |
