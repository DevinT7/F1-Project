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

### Known limitation: mid-lap accuracy is about ±40–60 ms, not ±0.02 s

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

Options considered (not implemented, pending a decision):
- **Anchor the normalization at the sector lines.** Use piecewise-linear normalization
  through each lap's own S1/S2 line distances, from its own official sector times. In a
  held-out test (anchor S1, measure at S2) the race median fell from 59 ms to 38 ms and
  Q from 44 ms to 40 ms. It's cheap, but the gain is modest, and the sector check
  becomes true by construction.
- **Align on X/Y position instead of integrated distance.** Project each sample onto
  the reference lap's path. This attacks the cause, but it's a larger change.

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

### Known issue (not from this phase)

`frontend/src/api.ts` fails `tsc` on `import.meta.env` because the project has no `vite-env.d.ts`.
The error already exists on `main`. The usual fix is to add `src/vite-env.d.ts` containing
`/// <reference types="vite/client" />`. That belongs to the frontend owner and isn't done here.

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
