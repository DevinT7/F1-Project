# Progress log

## Phase 2 — Lap alignment and delta engine (P1, 2026-10-12)

Code: `backend/app/processing/` (`alignment.py`, `delta.py`, `models.py`, `errors.py`).
The synthetic known-answer generator is `backend/tests/fixtures/synthetic.py`.
Entry point:

```python
from app.processing.delta import compare_laps, sector_check
from app.processing.models import LapData

ref = LapData.from_repository(repo, "2024_05_Q", "VER", 18)
tgt = LapData.from_repository(repo, "2024_05_Q", "ALO", 15)
cmp = compare_laps(ref, tgt, step=1.0)   # cmp.delta_s, cmp.speed_diff_kph, cmp.quality, ...
diag = sector_check(cmp)                 # independent sector-boundary cross-check
```

The algorithm is implemented as specified: clean, then linear length normalization,
then a 1 m grid, then linear interpolation for continuous channels and
previous-sample steps for gear, brake and DRS. The delta comes from recorded
timestamps; there is no 1/speed integration fallback. Smoothing is display-only.

### Acceptance

| Criterion | Result |
|---|---|
| Known-answer and antisymmetry tests | Pass. The delta matches the exact synthetic t(d) within 2 ms everywhere. Antisymmetry holds within 1e-6 s. |
| Real data: final-delta error ≤ 0.02 s for ≥ 95 % of valid pairs | **100 %** of 6000 pairs (3000 from 2024 R5 Q, 3000 from R). No comparison raised an error. |
| mypy strict, ≥ 90 % coverage on `processing/` | mypy is clean. Coverage is **98 %**. |
| Benchmark < 50 ms per comparison at 1 m | **~1.0 ms median, ~2.4 ms p95** (`python -m scripts.bench_alignment`, Python 3.11, Intel Family 6 Model 189 laptop). |

### Final-delta error distribution (real data, `python -m scripts.alignment_report`)

| Session | Valid laps | Pairs | Median \|error\| | p95 | Max |
|---|---|---|---|---|---|
| 2024_05_Q | 92 | 3000 | 0.000000 s | 0.000000 s | 0.000000 s |
| 2024_05_R | 757 | 3000 | 0.000000 s | 0.000000 s | 0.000000 s |

That every error is exactly zero is expected, and it's worth knowing why: FastF1's
`Lap.get_telemetry()` interpolates the first and last samples onto the lap start and
end. So every lap's telemetry duration equals its official `LapTime` (checked on all
92 valid Q laps). On FastF1 data the final-delta check therefore guards against bugs
in our own interpolation, not against data problems. It still catches broken
telemetry and timing (tested with synthetic laps).

### Known limitation: mid-lap accuracy was ±40–60 ms (now ±30–35 ms with X/Y, see follow-up below)

The sector cross-check is the real independent test. It **fails most of the time on
real data**:

| Session | Sector-boundary \|discrepancy\| median | p95 | All boundaries within 0.03 s |
|---|---|---|---|
| 2024_05_Q | 0.041 s | 0.136 s | 16 % of pairs |
| 2024_05_R | 0.064 s | 0.195 s | 9 % of pairs |

Here's the diagnosis. The physical sector lines sit at different FastF1 `Distance`
values from lap to lap. The standard deviation is 5–8 m raw, and still 3.7–5.6 m after
linear length normalization. At ~70 m/s that's 50–80 ms. Same-driver pairs show it
too (median 34–48 ms), so it isn't racing-line differences. It's speed-integration
drift that varies along the lap. A single linear scale factor corrects it only at the
finish line, which is exactly where we check.

Here's what that means downstream. The delta curve's **shape** is reliable (synthetic
tests). Its **endpoint** is exact. Its **mid-lap absolute value** carries tens of ms of
position-alignment uncertainty. Corner-level time-loss numbers in later phases
should be reported with that uncertainty, or with sector deltas as the anchor.

### Follow-up (2026-10-13): X/Y alignment, blended with distance

The X/Y option was implemented in `alignment.xy_align` and is on by default (`compare_laps(xy=True)`).

**How it works:**
- Each sample of the target lap is projected onto the reference lap's X/Y path. The search only
  covers ±60 m of the sample's length-normalized distance, so it can't snap to a neighbouring
  part of the track.
- The projection gives the reference distance of the same physical point. The difference from
  the target's own distance is low-pass filtered over 150 m.
- That correction is applied **at half weight**.
- The start and finish stay pinned at the timing line, so the final-delta check is unchanged
  (still exactly 0 s on both sessions).
- Samples that don't fit (more than 25 m off the path, or a 40 m jump) take their neighbours'
  correction. When X/Y is missing or mostly implausible, the method falls back to plain
  distance alignment.
- Every comparison reports `quality.alignment` (`xy` or `distance`) and `xy_correction_rms_m`.

**Why half weight.** X/Y alone was *worse*: the Q median went from 41 ms to 46 ms. Measured at
the official sector-line crossings, X/Y positions are scattered as much as integrated distance
(sd 3.7–6.1 m vs 3.5–3.8 m), and the two errors are **uncorrelated** (r = −0.20 … +0.18). So
the error isn't a shared timestamp problem, and neither source is clean. Averaging two
independent, similar-sized errors beats either one. On Q, weights 0.4–0.6 and smoothing
150–500 m form a flat optimum, and the middle of it (0.5, 150 m) was chosen.

**Results.** Tuned on 2024_05_Q only. **2024_05_R was held out** and run once with the chosen
setting. 3,000 pairs each, `python -m scripts.alignment_report --key … [--no-xy]`.

| Session | Method | Sector-boundary median | p95 | Max | All boundaries within 0.03 s |
|---|---|---|---|---|---|
| 2024_05_Q | distance (before) | 0.041 s | 0.136 s | 0.62 s | 16.0% |
| 2024_05_Q | **X/Y blend** | **0.028 s** | 0.099 s | 0.49 s | 31.4% |
| 2024_05_R (held out) | distance (before) | 0.064 s | 0.195 s | 0.40 s | 9.2% |
| 2024_05_R (held out) | **X/Y blend** | **0.036 s** | 0.106 s | 0.23 s | 23.0% |

Mid-lap position uncertainty falls by about 30–44%, from ±40–60 ms to about ±30–35 ms, but not
to the original ±0.02 s goal. The remaining error is the noise of two independent position
sources. The sector check stays an independent test, because nothing is anchored to the
sector lines.

**Effect on findings.** Corner time losses move as the alignment improves. In the reviewed
ALO-vs-VER snapshot (regenerated deliberately), individual corners shift by up to about 0.1 s,
e.g. T9 −0.011 → +0.071 s and T13 +0.128 → −0.013 s. The S1 boundary error went from −24 to
+7 ms while S2 went from +4 to +41 ms. Single pairs still carry tens of milliseconds at any
boundary, so corner losses below about 0.05 s should be presented as low confidence under
either method. The lap total is unaffected.

**Cost:** about 1.5 ms per comparison (2.1 → 3.6 ms for `compare_laps`).

### Other notes

- **Resampling invariance** holds within 0.5 km/h and 5 ms between 10 Hz and 25 Hz
  input. At 4 Hz the samples are ~21 m apart at 315 km/h, and linear speed
  interpolation through a 315→95 km/h braking zone is off by up to ~2.5 km/h. That's
  a limit of sampling, not of alignment. FastF1 merged telemetry is ~7.5 Hz.
- **Failure handling.** Duplicate timestamps and unsorted samples are cleaned. A
  non-advancing distance at > 5 km/h is dropped as a glitch (counted in
  `quality.dropped_glitches`), and stationary samples get an epsilon nudge (counted in
  `quality.repairs`). These raise errors instead: fewer than 200 usable samples or a
  duration off by more than 0.15 s (`LapInvalidError`), a length mismatch over 3 %
  (`LapInvalidError`), and a final-delta mismatch over 0.02 s (`InsufficientDataError`).
  On the real data, no repairs or glitch drops were needed.
- Smoothed `delta_rate_smoothed` (Savitzky–Golay, 25 m, order 2) is for display only.
  `delta_rate` (the unsmoothed gradient) is kept for any numeric use.

## Phase 4 — Backend API, /api/v1 (P1, 2026-10-12)

Code: `backend/app/api/` (`schemas/`, `routes/`, `services.py`, `deps.py`, `errors.py`), mounted in
`app/main.py` next to the existing `/api` routes (unchanged). Swagger UI: `/docs`.

- **Endpoints (11):** `GET /health`, `GET /ready`, `GET /seasons`, `GET /seasons/{year}/events`,
  `GET /sessions/{key}`, `POST /sessions/{key}/ingest`, `GET /sessions/{key}/drivers`,
  `GET /sessions/{key}/laps?driver=`, `GET /sessions/{key}/fastest-laps`, `GET /sessions/{key}/corners`,
  `POST /comparisons`. Session keys accept `2024-05-Q` or `2024_05_Q`.
- **Errors:** every error body is `{"error": {code, message, status_code, request_id[, details]}}`. The
  legacy `/api` routes now carry `request_id` too, an additive change. The status mapping is
  session/driver/lap not found → 404, `lap_invalid` / `insufficient_data` / `validation_error` → 422,
  `telemetry_unavailable` → 424, `payload_too_large` → 413, `rate_limited` → 429 (the limiter is
  still a stub), and anything unexpected → 500 with a generic message. Stack traces are only
  logged, never returned.
- **Comparisons:** the analysis always runs at 1 m. `resolution_m` (1–10, default 2) only thins the
  arrays: 2,701 points for Shanghai, 364 KiB of JSON, about 99 KiB gzipped (level 6). Booleans are
  JSON booleans. A lap that isn't valid for comparison is rejected with its `invalid_reason`.
- **Caching:** there are two in-process LRUs, one for per-session lap/driver tables and one for
  finished comparisons (stored as ready-to-send JSON). Both are keyed on the manifest's
  `ingested_at`, so a re-ingest invalidates them. Redis replaces this in Phase 8.
- **OpenAPI:** `backend/openapi.json` is the reviewed snapshot. A test fails on unreviewed
  changes, and `python -m scripts.export_openapi --check` does the same for CI.
- **Types:** `frontend/src/api/types.ts` is generated by `openapi-typescript` (`make gen-types`,
  or on Windows `python -m scripts.export_openapi` followed by `npm run gen-types`). The
  generated types compile under `--strict`.

### Performance (`POST /comparisons`, 2024 R5 Q VER vs ALO, default 2 m, gzip, 100 requests per run)

| Path | Median | p95 |
|---|---|---|
| Uncached (session stored on disk, comparison computed) | 133–135 ms | **196–215 ms** |
| LRU hit | 44 ms | 55–62 ms |

Target met: p95 < 300 ms. Measured in-process through Starlette's TestClient on the same
laptop as the Phase 2 benchmark. The first version ran at **301 ms p95**. Three fixes brought it
down:

- The route returns pre-rendered JSON, which skips FastAPI's re-validation and
  `jsonable_encoder` pass (about 35 ms).
- Lap resolution reads from in-memory session tables instead of Parquet on every request.
- The middleware is pure ASGI instead of `BaseHTTPMiddleware`, which had been re-streaming the
  large body (about 40 ms).

The test suite asserts p95 < 300 ms over 20 uncached requests.

### Known issue (not from this phase): resolved in Phase 5

`frontend/src/api.ts` failed `tsc` on `import.meta.env` because the Vite project had no
`vite-env.d.ts`. Phase 5 moved the frontend to Next.js and removed that file. `npm run
typecheck`, `lint` and `test` are clean as of the 2026-10-13 audit.

## Phase 5 — Frontend dashboard (P1, 2026-10-12)

`frontend/` is now a **Next.js 16 App Router** app (TypeScript strict, Tailwind 4, TanStack
Query, Zustand, Plotly basic bundle). It merges the original Vite dashboard into the Phase 5
spec.

**Carried over from the original frontend** (`src/Dashboard.tsx`, `src/styles.css`,
`design/raceiq-dashboard.html`):
- The pit-wall design: RIQ sidebar and nav, eyebrow plus serif-italic title, selector strip,
  KPI strip, cards, sector-delta rows, and the corner card beside the track map.
  The original palette is the light theme. A matching warm dark palette is the default.
- The live per-row value readouts next to the telemetry rows, now driven by hover.
- **Demo data** (`?demo=1`): serves real API responses exported by
  `backend/scripts/export_frontend_fixtures.py`, so it needs no backend.
- The `/api` dev proxy, now a Next rewrite (`BACKEND_URL`, default `http://localhost:8000`).
- The Tire and AI Engineer cards. They're placeholders behind
  `NEXT_PUBLIC_FEATURE_TIRES` / `NEXT_PUBLIC_FEATURE_ENGINEER`, as the spec requires.

**Removed:** the Vite entry files and the old client for the legacy `/api` endpoints
(`src/Dashboard.tsx`, `api.ts`, `types.ts`, `mock.ts`, `styles.css`, `main.tsx`,
`index.html`, `vite.config.ts`). Their roles are taken over by the components and
`src/lib/` (the generated-type client, `src/fixtures/api/` for demo data, and
`globals.css`).

**Structure:**
- `src/lib/`: typed API client derived from `src/api/types.ts`, URL state, formatters
  (with a locked sign convention), colour scale and contrast, track geometry, store, query hooks.
- `src/components/`: SessionSelector, LapPicker, ComparisonHeader, TelemetryStack, TrackMap,
  CornerPanel / FindingsList / SectorCard, shell, error boundary.

**Shareable URLs:** `/?s=2024-05-Q&ref=VER:fastest&tgt=ALO:15[&res=4][&demo=1]`. The page
renders from the URL alone, and every selection writes back to it.

### Acceptance

| Criterion | Result |
|---|---|
| A shared URL reproduces the same comparison | ✅ e2e: load the URL, reload, and get the same gap (+0.488 s) and the same pickers |
| Hovering any chart moves the cursor on all charts and the map marker | ✅ one shared x-axis plus a store-driven overlay cursor and map marker (e2e and manual) |
| The delta chart's end value equals the displayed lap gap | ✅ 0.488 vs "+0.488 s" (e2e asserts it) |
| Map colours agree with the findings' signs (3 corners) | ✅ T16 ▲ red, T11 ▼ green, T9 ● grey (component test and `docs/screenshots/README.md`) |
| Lighthouse accessibility ≥ 90, no console errors | ✅ **accessibility 100**, best practices 96, console-errors audit clean |
| `make check-frontend` green | ✅ typecheck + ESLint + 84 Vitest tests |

### Tests

- **Vitest (84 tests):** formatters (sign convention), URL parse/serialize round-trip, colour
  scale including the ±0.02 s neutral band, contrast/luminance adjustment, segment-to-path
  (on the real lap), API client and error envelope, Demo mode.
- **Component tests with MSW** (handlers serve the exported real responses): selector cascade
  through to URL, shared-URL load, invalid-lap flag, error toast with code and request_id,
  network error, corner click and finding click updating the panel, map/finding sign
  agreement, Demo mode with the network blocked.
- **Playwright e2e (2 tests):** real backend on the fixture repo plus a production build. Load
  the URL, charts render (11 traces), hover moves the map marker, clicking a corner fills the
  panel, delta end equals the gap, no console errors, and a reload reproduces the comparison.

### Performance (production build, 2,701 points × 11 traces across 5 subplots)

- Charts are interactive about 2–3 s after a cold load (Plotly is lazy-loaded).
- Hover sweep of 120 moves: **0 long tasks**. Each move takes about 50 ms to the next painted
  frame, measured through Playwright, which includes the IPC round trip and two
  `requestAnimationFrame` waits.
- The first version moved the cursor as a Plotly shape. Plotly treats a shape edit as a
  `calc` edit that recomputes every trace, which cost 82 ms per move and produced 78 long
  tasks. The cursor and the selection band are now DOM overlays positioned from the x-axis
  mapping, so hover never touches Plotly data or layout.

### Environment notes

- `next build` / `next dev` default to Turbopack. In this sandbox, Turbopack's PostCSS worker
  fails to spawn (Windows error 0xc0000142), so the e2e config uses `next build --webpack`,
  and `next dev --webpack` works for local dev here. Plain `next dev` should be fine on a
  normal machine.
- `agentRules: false` in `next.config.ts` stops `next dev` from writing an `AGENTS.md` into the repo.
- TypeScript is pinned to 5.x. The newest release (7, the native port) conflicts with
  `openapi-typescript`'s peer range.

## Phase 6F — Frontend tyre panel (P1, 2026-10-13)

Code: `frontend/src/lib/tires.ts` (state selection, formatters, window geometry, slider bounds,
text summary), `components/tire-section.tsx`, `tire-panel.tsx`, `tire-chart.tsx`,
`stint-timeline.tsx`, `qualifying-tire-chart.tsx`. Hooks: `useTirePrediction` and `useStints` in
`lib/queries.ts`. URL: `&tyre=AAA&asof=40` (shareable like comparisons). The tyre flag is on by
default; `NEXT_PUBLIC_FEATURE_TIRES=0` turns it off. The Phase 7 input contract is
`docs/tire_facts_contract.md`.

### What the panel shows

- **StintTimeline:** one bar per stint in the standard compound colours *and* labelled with the
  compound letter and name, laps and tyre age. Clicking a stint selects it and sets "as of" to its
  last lap, never the race's final lap.
- **Race sessions:**
  - **Header:** compound badge, tyre age, clean laps used.
  - **"As of lap N" slider:** limited to laps with at least 5 clean laps. Its note says the forecast
    only uses laps up to N.
  - **Degradation card:** "0.034 s/lap, 95% CI 0.029–0.039", with the combined-adjusted-trend
    explanation.
  - **Pit-window card:** labelled a heuristic, and the window is also available as text.
  - **Chart:** observed clean laps, hollow excluded laps (toggle), model fit, forecast with its
    80% band, the threshold line, the shaded window with the central lap, and the current-lap
    marker. A text summary is attached via `aria-describedby`.
  - **Footer:** confidence with its reasons, assumptions, remaining competitive laps, and model
    name/version/trained-through.
- **Other sessions:** "Tire forecasting requires a race; showing raw stint data", plus the stints.
  Qualifying sessions also get the lap-time-vs-tyre-age chart ("Observed laps — not a
  prediction"), with fresh and used sets marked.
- **States:** loading skeleton, insufficient data ("2/5 clean laps" progress), wet conditions,
  model unavailable (503, with request id), non-race session, and error (panel card plus the
  global toast with request id). The expected 503 and non-race responses are panel states, so they
  don't raise toasts (`meta.handledCodes`).

### Backend additions

These are additive fields on `TirePrediction` (`docs/tire_api.md`). The synthetic race fixture
`2024_99_R` (`scripts/build_synthetic_race_fixture.py`) gives every state a real source, and its
"distinctive later laps" back the cut-rule checks: AAA's laps from 41 on are about 4 s slower. The
frontend fixtures for all of it are real API responses (`scripts/export_frontend_fixtures.py`).

### Acceptance

- **Five panel states, plus loading, verified visually:**
  `docs/screenshots/tires/0-loading.png` … `5-wet.png`. Regenerate with
  `SCREENSHOTS=1 npx playwright test tire-screenshots`. The 503 state replays the API's exported
  503 body; the rest are live.
- **"As of" never shows later laps.** This is checked at three levels:
  - Vitest checks all 25 exported "as of" responses: no lap after N, and no distinctive lap time
    while N ≤ 40.
  - A component test moves the slider and checks the listed observed laps.
  - Playwright reads the plotted trace at N = 40 and N = 35: max x equals N and every y is under
    94 s.
- **The contract matches the API.** `tests/test_tire_facts_contract.py` diffs field names in
  both directions and proves it catches drift.
- **`make check`:** a new target (`test typecheck openapi-check check-frontend`). This machine has
  no `make`, so its commands were run directly and are green.

## Phase 7 — AI race engineer (P1, 2026-10-13)

| Part | Status |
|---|---|
| 7A FactSheet | done; human gate approved |
| 7B validator | done; corpus, mutation test, 10 human-gate cases |
| 7C template report | done; review changes applied |
| 7D LLM report generation | code done (client, prompts `report-v1`, generator, eval script). **Live eval pending: no LLM API key is configured yet** |
| 7E follow-up Q&A | code done (router, slices, evidence, templates, conversation, prompt `qa-v1`). **Live 40-question check pending: same key** |
| 7F API, frontend, observability | code done (endpoints, cache, kill switch, report card, chat, metrics, E2E safety test). **Final live evaluation pending: same key** |

### 7E offline results

- **Router:** 106 phrasings, 100% routed (≥ 90% required).
- **Templates:** every intent's template answer validates on all 5 fixtures.
- **Offline run:** the 40-question run over 8 stored comparisons (7 races, the one qualifying session in `data/`) routed every question to its intended intent.
- **Transcripts:** the ten transcripts are in `data/ai_eval/qa_<timestamp>/transcripts.md`.
- **FactSheet:** gained `conditions.traffic_data`, so the goldens and report snapshots were regenerated.
- **Shared loop:** the 7D retry/fallback loop moved to `app/ai/guarded.py`, used by both reports and Q&A.


### 7D live evaluation

⏳ **PLACEHOLDER — API KEY NEEDED.** Set `LLM_PROVIDER`, `LLM_MODEL` and `LLM_API_KEY` in `backend/.env`, then run `python -m scripts.eval_report_quality --live`. The script replaces this section with the outcome table (target ≥ 95% valid within one retry, ≥ 80% first try), violations by rule, latency, tokens and cost. Then grade the ten sampled reports (accuracy ≥ 4/5).

### 7E live Q&A check

⏳ **PLACEHOLDER — API KEY NEEDED.** Same three variables, then run `python -m scripts.eval_qa_quality --live`: 40 questions over 8 comparisons (target ≥ 95% valid within one retry). The script replaces this section with the results. Then review the ten LLM transcripts, adversarial ones included.

### 7F results (no API key needed)

- **Endpoints:** `POST /api/v1/engineer/report`, `POST /api/v1/engineer/ask`, `GET /api/v1/engineer/facts/{hash}` (OpenAPI and TS types regenerated); admin `GET /api/v1/engineer/metrics` (`docs/runbook.md`).
- **End-to-end safety** (`frontend/e2e/engineer.spec.ts`): Playwright runs the real backend with a scripted fake LLM that injects "0.35 s" and "tyre wear" into every draft. Neither reaches the DOM (report, every fact popover, chat); the card shows the validated template badged "Template summary", with the rejected rules (`ungrounded_number`, `unsupported_causal_claim`, `ungrounded_label`).
- **Kill switch:** with `ENGINEER_LLM_ENABLED=false`, templates are served (`template_disabled`) and the fake LLM records zero calls.
- **Cache:** an identical request hits; changing the prompt version or the model misses; template reports are cached under their own key; a fallback is never cached under the LLM key.
- **Validation:** an empty, control-only or over-300-character question is a 422. Control and format characters are stripped, and the question never reaches the system prompt.
- **Latency:** the template path is 54 ms with the comparison warm (27 ms on a cache hit); the test asserts < 200 ms. The LLM path keeps a loading state in the card and chat, and its p95 is reported by the live eval.
- **Metrics:** every outcome path is counted, plus violations by rule, latency, tokens, cost, cache hit ratio, intents and out-of-scope rate.
- **Frontend:**
  - the report card has badges with fallback reasons, keyboard/hover fact popovers, an always-visible limitations block, copy, regenerate (10 s cooldown) and the footer note;
  - the chat has suggestion chips, a 300-character limit, a loading state, an inline error with the request id, neutral refusals and a live region;
  - clicking a bullet highlights its segment.
- **Docs:** `docs/runbook.md`, `docs/limitations.md` (question retention policy), `docs/demo_script.md` (the human gate: three comparisons).

### 7F final live evaluation

⏳ **PLACEHOLDER — API KEY NEEDED.**

1. Set `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` (and optionally the two price variables) in `backend/.env`.
2. Run `python -m scripts.eval_report_quality --live` and `python -m scripts.eval_qa_quality --live`.
3. Record here: first-try pass rate, post-retry pass rate, fallback rate, cost per report, and LLM-path p95 latency (target < 8 s).
4. Then walk through `docs/demo_script.md` with the LLM on.


## Phase 8 — Persistence, caching, auth, background jobs (P1, 2026-10-14)

API key: **not needed** (all services run locally).

| Acceptance criterion | Status | Evidence |
|---|---|---|
| Entire app runs via `docker compose up` with seeded sample data | ✅ | The stack is TimescaleDB, Redis, SeaweedFS (S3), seed, backend, RQ worker and frontend. The seed applies migrations 0001→0002, loads 2024 R5 Q + the synthetic race into Postgres, and publishes the three tyre models to S3 and `model_registry`. 15/15 end-to-end checks passed through the frontend proxy (below). |
| Both repository implementations pass the same contract suite | ✅ | `tests/test_repository_contract.py`: 20 behaviour tests × {Parquet, Postgres}. |
| Identical concurrent comparison requests compute once | ✅ | Unit: 16 threads, and 3 "workers" sharing Redis → 1 computation. Stack: 10 concurrent identical `POST /comparisons` → exactly one `meta.cached=false`. |
| Unauthorized access to admin/engineer endpoints → 401/403 | ✅ | `tests/test_auth.py::test_role_enforcement`. Stack: ingest with no token → 401, as a user → 403, as admin → 202. Engineer with no token → 401. |
| Migrations reversible and CI-tested | ✅ | `tests/test_db_migrations.py` runs up→down→up and checks schema.py against the migrations; the CI workflow also runs `alembic upgrade → downgrade base → upgrade`. |

**Built**

- **Storage.**
  - `PostgresRepository` (SQLAlchemy 2.0 Core, Alembic) with a one-transaction replace (idempotent re-ingest) and `COPY` for telemetry.
  - Telemetry lives in a TimescaleDB hypertable with compression.
  - `scripts/migrate_parquet_to_postgres.py` migrated the real `data/` (25 sessions, ~20 M samples) in 9.5 min.
  - A comparison takes 71 ms on Postgres vs 61 ms on Parquet.
- **Cache.** `app/caching.py`: memory or Redis, with versioned keys `raceiq:v1:{kind}:…`, TTLs, invalidation on re-ingest, single-flight, and graceful degradation when Redis is down. Comparisons, FactSheets, engineer reports, schedules and conversations (`RedisConversationStore`) are shared by every worker.
- **Jobs.**
  - RQ executes; a lifecycle runner of our own handles progress, retries with backoff and permanent failures.
  - `POST /sessions/{key}/ingest` → 202 + job, `GET /jobs/{id}`, and the frontend polls real progress.
  - Training job: `POST /admin/models/train`.
- **Object storage.** S3 via boto3 (SeaweedFS locally, since MinIO images are no longer pullable). `DbModelRegistry` reads artifacts by object key with SHA-256 verification.
- **Auth.**
  - argon2id, 15-min Bearer tokens, a single-use rotated refresh cookie with reuse detection, CSRF header + Origin check, roles.
  - Saved comparisons under `/me/analyses`.
  - Redis token-bucket rate limits returning 429 + `Retry-After`.
  - Frontend: sign-in dialog, account menu, engineer sign-in prompt, admin-only ingest.
- **Docs and CI.** `docs/security.md`, `docs/decisions.md` (storage, cache, RQ, object storage, auth), `.github/workflows/ci.yml` (service containers, migration round trip, mypy, OpenAPI check, tests, pip-audit, npm audit, Playwright), and `make up / down / migrate / seed / logs`.

**Found and fixed on the way**

- `requirements.txt` could never resolve on a fresh install: `pyarrow==26.0.0` conflicts with `mlflow 3.17`, which requires <26. The pins now match the tested environment (pyarrow 25.0.1, redis 8.1.0); the Docker build caught it.

**Test totals:** backend 1116 passed (mypy strict, 107 files); frontend 144; Playwright 7. The engineer e2e signs in through the real dialog.
