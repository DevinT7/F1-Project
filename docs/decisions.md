# Decisions log

## Phase 1 — Data ingestion and caching (P1, 2026-10-12)

Code: `backend/app/ingestion/` (adapter, models, normalize, service, CLI) and
`backend/app/storage/repository.py`. Run from `backend/`:

```bash
python -m app.ingestion ingest --year 2024 --round 5 --session Q [--force]
python -m app.ingestion season --year 2024 --sessions Q,R
python -m app.ingestion events --year 2024
python -m app.ingestion list
```

### Where the plan and the repo differ

| Plan says | What we did | Why |
|---|---|---|
| `raceiq/ingestion/…`, `raceiq/storage/…` | `backend/app/ingestion/…`, `backend/app/storage/…` | The repo already has the `backend/app` package; P2 and P3 put their work there too. |
| `python -m raceiq.ingestion …` | `python -m app.ingestion …` (run from `backend/`) | Same reason. |
| `make test` | `cd backend && pytest` (network tests are excluded by `pytest.ini`) | The repo has no Makefile. |
| `session.load(..., messages=False)` | **`messages=True`** | FastF1 3.5.3 only fills in `Laps.Deleted` from race-control messages (`core.py::_set_laps_deleted_from_rcm`). Without them every lap reads `Deleted=None`, so the "not Deleted" validity rule can't work. Loading messages adds well under 1 s. |
| Fixture: 2 drivers × 3 laps | **3 drivers × 3 laps** (VER, PER, ALO), 1.0 MB | The acceptance criterion needs fastest-lap matches for ≥ 3 drivers in that fixture. |
| "only module that imports fastf1" | Also routed the legacy chart client (`app/clients/fastf1_client.py`) through `ingestion.fastf1_client.import_fastf1()` | Keeps a single import site. `ml/build_dataset.py` (P3's standalone pipeline) still imports fastf1 directly and is outside the isolation test's scope. |

### FastF1 3.5.3 schemas, probed on 2024 R5 (Chinese GP) Q and R

- **`session.laps`** (31 columns). Everything the plan lists is present. Dtypes to watch:
  `LapNumber`, `Stint`, `TyreLife` and `Position` are **float64** (NaN-able), not int.
  `DriverNumber` is a **string**. `Deleted` is **object** (bool, or None without
  messages). `TrackStatus` is a concatenated code string such as `"12"` or `"1267"`.
  There are extras we don't keep: `SpeedI1/I2/FL/ST`, `IsPersonalBest`, `Sector*SessionTime`,
  `LapStartDate`, `FastF1Generated`. Quali: 279 laps, 114 with no LapTime (out/in laps).
  Race: 1032 laps, 5 deleted for track limits.
- **`lap.get_telemetry()`** (18 columns): all planned channels are present (`Speed, RPM,
  nGear, Throttle, Brake, DRS, X, Y, Z, Time, SessionTime, Distance`). `Brake` is
  **bool**. `Time` is lap-relative and starts at 0. The first row is
  `Source="interpolation"`. `Distance` was already monotonic in every lap we checked.
  Extras: `Date`, `DriverAhead`, `DistanceToDriverAhead`, `RelativeDistance`, `Status`,
  `Source`. About 700–1000 samples per lap.
- **`session.weather_data`**: `Time, AirTemp, Humidity, Pressure, Rainfall, TrackTemp,
  WindDirection, WindSpeed`. **`WindSpeed` is m/s** (`_api.py` docstring), so we store
  `wind_kph = WindSpeed × 3.6`. One sample per minute.
- **`get_circuit_info().corners`**: `X, Y, Number, Letter, Angle, Distance`. `Letter`
  is `""` when there's no letter, which we store as None. `rotation` is a float (237.0
  at Shanghai). The raw table is kept in `corners_raw.json`.
- **`session.results`**: `Q1/Q2/Q3` are timedeltas. `TeamColor` is hex with no `#`
  (we add one). `Position` and `GridPosition` are float.
- **`session.date`** is a **naive** Timestamp, but its value is UTC (07:00 for a 15:00
  local Shanghai start). We attach UTC without shifting it. Schedule
  `Session{n}DateUtc` columns are treated the same way.
- `session.total_laps` is None for qualifying and 56 for the race. It's stored as
  `n_laps_scheduled`.

### DRS normalization

The FastF1 3.5.3 docs (`fastf1/_api.py`, `car_data`) say only: *"DRS (int): 0-14 (Odd
DRS is Disabled, Even DRS is Enabled?)"*. Codes observed across all 20 drivers' car
data in 2024 R5 Q: `0, 1, 2, 3, 8, 9, 10, 11, 12, 14`. The commonly used decoding is
0/1 = off, 8 = eligible (detected, not yet open), 10/12/14 = flap open, and odd codes
= disabled. We store the raw `drs_code` and set
**`drs_open = drs_code ∈ {10, 12, 14}`** (`normalize.DRS_OPEN_CODES`). The plan's
"≥ 10" rule would wrongly count code 11 as open. It appeared in only 26 samples, but
FastF1's own odd = disabled note excludes it.

### Lap validity (`LapRecord.is_valid_for_comparison`, `invalid_reason`)

Rules are checked in this order, and the first failure becomes the reason:
`no_lap_time` → `not_accurate` → `deleted` → `no_telemetry` → `too_few_samples`
(fewer than 200) → `distance_out_of_range`. The last one means the lap's telemetry
distance is more than ±3 % from the session median, where the median is taken over
laps that have both a lap time and telemetry. `deleted=None` (unknown) does **not**
invalidate a lap. On 2024 R5 Q this leaves 92 of 279 laps valid, and
`get_fastest_valid_lap` matches the official best of Q1/Q2/Q3 within 1 ms for **all
20 drivers**.

### Storage

- Each session is written to `data/{year}_{round:02d}_{code}/` at the repo root
  (gitignored, `RACEIQ_DATA_DIR` overrides it). Telemetry goes in
  `telemetry/{DRV}_{lap:03d}.parquet` and is read lazily per lap. Laps, weather and
  corners are Parquet (zstd). Session, drivers, raw corners and the manifest are JSON.
- Every file is written atomically (temp file + `os.replace`). A session is staged in a
  sibling `.staging-*` directory and swapped in, and `manifest.json` is written last.
  A crash at any point leaves the previous copy intact, or leaves no session at all.
- `models.SCHEMA_VERSION` (currently 1) is stored in the manifest. A mismatch makes
  `has_session` return False, which triggers a re-ingest. Bump it whenever a stored
  model changes.
- The schedule is cached at `data/_schedule/{year}.json`. Past seasons are cached
  forever. The current and future seasons refresh after 24 h.

### Performance (measured on this machine)

- Cold FastF1 load: 29 s for Q and 45 s for R (downloads). A warm cache takes about 4 s.
- `Lap.get_telemetry()` takes about **0.5 s per lap** and dominates ingest time: full Q
  ≈ 2.5 min (279 laps, 14 MB stored); full R ≈ 7.5 min (1032 laps, 757 valid, 5 deleted, 40 MB).
- A no-op re-ingest takes about 1.0 s wall, interpreter startup included.
- Follow-up if season batches get too slow: merge `car_data` and `pos_data` once per
  driver and slice by lap start/end, instead of calling `get_telemetry()` per lap.

### Earliest supported year

Only 2024 has been verified so far. FastF1 has timing and telemetry from 2018. Re-test
older seasons before claiming support for them. Missing telemetry is already handled:
the lap is marked `no_telemetry` and nothing crashes.

## Phase 6A — Tire dataset (P1, 2026-10-12)

Code: `backend/app/tires/` (`config.py`, `exclusions.py`, `dataset.py`, `stints.py`,
`report.py`). The plan's `raceiq/tires/` maps to `backend/app/tires/`, following the same
convention as Phases 1–5. The CLI is `python -m app.tires.dataset build --seasons 2023 2024`.

### Track-status codes (verified in FastF1 3.5.3, `fastf1/_api.py::track_status_data`)

| Code | Meaning | Treated as |
|---|---|---|
| `1` | Track clear | green |
| `2` | Yellow flag (sectors unknown) | non-green |
| `3` | "??? Never seen so far, does not exist?" (from the FastF1 docs) | non-green |
| `4` | Safety car | non-green, neutralization |
| `5` | Red flag | non-green, neutralization, restart detection |
| `6` | Virtual safety car deployed | non-green, neutralization |
| `7` | VSC ending | non-green, neutralization |

A lap's `TrackStatus` concatenates every code seen during the lap (`"12"`, `"1267"`), so the
rule treats it as a **set of characters**. A lap is kept only if that set is exactly `{"1"}`,
which means `"11"` is green and `"12"` is not. A missing status is not green.

### Rule interpretations not spelled out in the plan

- **`lap_1_or_restart`:** the restart lap is decided per driver. It is the lap whose previous
  lap (same driver) carried code `5` while this lap does not. Red-flag laps themselves fall to
  `non_green_track_status`.
- **`inaccurate_or_deleted`:** `Deleted = None` (unknown) does not count as deleted.
- **`stint_outlier`:** a stint whose MAD is 0 is left alone. A zero spread gives no basis for
  judging outliers, and with a zero threshold anything slower than the median would be dropped.
- **`laps_since_neutralization`:** counts from the most recent earlier lap number on which
  **any** driver carried code 4, 5, 6 or 7. Before the first one, the sentinel is 999. The lap
  number is a proxy for the leader's timeline, so it is approximate for lapped cars.
- **`prev_lap_time_s`:** the previous **kept** lap in the same stint, which can skip over excluded
  laps. It is null at the start of a stint. It may only be used as a feature where it would
  really be available, which means forecasting.
- **Stints endpoint `clean_laps`:** counts laps passing the lap-level rules (1–7).
  `model_eligible` then applies the stint-level rule (≥ 5 clean laps). This matches what the
  dataset keeps without reporting `clean_laps: 0` for a stint that simply has 4 clean laps.
- **Traffic:** there is no reliable gap data, so `traffic_known = False` on every row, and no
  traffic feature is derived.
- **Units:** `fuel_proxy = race_laps_total − lap_number` stays in laps. No kg conversion and no
  time-per-kg constant exist anywhere. `race_laps_total` falls back to the highest lap number
  when FastF1 has no scheduled distance.

## Phase 6D — Tree models, intervals, decision (P1, 2026-10-13)

Full write-up: `docs/tire_model_report.md`. Decisions and deviations:

- **Target:** trees predict lap time − mean(last 3 history laps), so every tree is a correction
  to persistence rather than a re-learning of each circuit's level.
- **"GradientBoosting (sklearn)"** is `HistGradientBoostingRegressor`, sklearn's recommendation
  at about 100k rows per fold.
- **Bar during development is relative:** MAE ≤ 0.9 × persistence and WF ≤ persistence, on the
  same 21 races. The absolute numbers (0.332 s / 0.390 s) were set on all 24 races and are
  checked only on the 3 held-out races.
- **80% intervals are judged against 72–88%,** 95% against 90–97% (`bar.coverage_range`).
- **Tree degradation:** the within-stint slope plus Baseline B's β_fuel. The plan's
  fuel-held-fixed counterfactual is arbitrary because `age_gain ≡ fuel_drop` in every row.
- **Tuned hyperparameters used outside the nested evaluation** (intervals, SHAP, holdout
  reference rows) come from one search over all 21 development races. The honest tuned
  estimate is the nested one.
- **Shipped:** Baseline B + split-conformal 80%, per the protocol (no model met the bar). The
  report recommends persistence + conformal instead, pending human review.

## Phase 6E — Prediction service and pit window (P1, 2026-10-13)

Full contract: `docs/tire_api.md`.

- **Servable artifacts are JSON coefficients + `.npz` residuals,** committed under `models/`
  with SHA-256 in `models/registry.json`. There are no pickles: loading cannot execute code, and
  it doesn't depend on statsmodels internals.
- **Production is `baseline_b_conformal`, the 6D protocol fallback.** `persistence_conformal` is
  registered as a candidate. Switching after the 6D review is
  `build_tire_registry --production persistence`.
- **Conformal residuals are calibrated per step up to 25 laps** (horizons 5, 10, 25), so every
  pit-window step has its own quantile.
- **The cut uses every driver's laps ≤ lap,** not just the driver's own, because the 6A rules
  need the field (neutralizations, restarts). The 6A rules run with `keep_short_stints=True` so a
  short stint reports its clean-lap count and returns `insufficient_data`, instead of vanishing.
- **Schema additions:** `reason`, `pit_window_reason`, and the pit-window fields `best_pace_s`,
  `threshold_pace_s`, `pace_basis` and `notes`, so every null and every number is explained.
  Optional `threshold` query parameter (X); the response cache key includes it.
- **The plan's 2·X uncertainty rule makes the window null in 95% of forecasts at X = 1.0 s.**
  Measured on 24 races; the window only appears in the last 5 laps. The rule is kept as
  specified, and the options are listed in `docs/tire_api.md` for review.

### 6E fixes after review (2026-10-13)

- **Production model:** Baseline B anchored on the last 3 laps (`PooledFixedEffects(anchor_laps=3)`).
  On the development races it is as accurate as persistence, and it is the only candidate with
  a trend for the pit window plus a degradation estimate. Persistence and the 6D Baseline B
  remain candidates. It has no clean holdout estimate (the holdout was used once in 6D).
- **Pit window v2 (`interval_crossing_v2`):** the 2·X gate is judged at the central crossing
  lap, not 25 laps out. That takes real-race window availability from 4.9% (v1, 6D model) to
  61.4%. Smoothed-pace intervals were tried and rejected: the step-25 uncertainty is genuine
  pace drift (1.57 s for a 5-lap mean vs 1.69 s per lap), so it is not noise to smooth away.
- **Windows whose threshold was reached before the cut say so** ("threshold already reached
  before the current lap") instead of reading as a fresh "pit next lap" call.

## Phase 2 follow-up — X/Y alignment (P1, 2026-10-13)

- **The target lap's distance is corrected by projecting its X/Y onto the reference path.** The
  correction is blended at 0.5 with the length-normalized distance and low-pass filtered over
  150 m. X/Y alone was worse than distance alone, because the two sources have independent,
  similar-sized errors.
- **Tuning used 2024_05_Q only.** On the held-out 2024_05_R the sector-boundary median went from
  64 to 36 ms. The details are in `docs/progress.md`.
- **The X/Y step is on by default.** `compare_laps(..., xy=False)` and
  `alignment_report --no-xy` reproduce the original method.
- **The reviewed findings snapshot was regenerated deliberately.**
- **The comparison latency test now excludes 2 warm-up requests and times 60 samples, not
  20.** It was flaky with or without X/Y.

## Phase 6F — Frontend tyre panel (P1, 2026-10-13)

- **The tyre section is independent of the lap comparison.** It renders for any ready session:
  races get the panel, other sessions the non-race notice with stints.
- **The panel's data comes only from the `/tires/predict` response for "as of" lap N.**
  Observed laps, fit and threshold are added to that response rather than taken from the
  whole-race laps endpoint, so the UI cannot leak a later lap by construction.
- **The non-race state is decided from the session code** (no request is made). The API's 422 is
  still handled if it ever arrives.
- **The synthetic race uses round 99 of 2024,** a round that doesn't exist. It sits in the
  shared fixture store, so the stored-session count test now expects 2.
- **Screenshots are opt-in** (`SCREENSHOTS=1`), so ordinary e2e runs never rewrite binaries.

## Phase 7A — FactSheet and contracts (P1, 2026-10-13)

- **Human gate passed 2026-10-13.** The printed FactSheet (`race_lec_pit_window`) was reviewed
  end to end and approved.
- **Every source value is a fact or a documented exclusion.** Findings exclusions: schema
  version, comparison id, finding id/kind. Tyre exclusions: the 6F contract's "Not exposed"
  table, plus the next lap's number, which is always `as_of_lap + 1`.
- **Null reasons are specific.** Differences say which driver lacks the event. Tyre nulls follow
  the contract's semantics: an open-ended window is a statement, not missing data.
- **Prompt size (for 7D/7E).** A FactSheet holds about 1,000 numeric facts (~300 KB). The model
  receives a compact rendering or the relevant slices; the validator checks against the full
  FactSheet.

## Phase 7B — Validator (P1, 2026-10-13)

- **Human gate (corpus review plus 10 added cases) completed 2026-10-13.** The user delegated
  the 10 cases (`h01`–`h10`). The corpus is 85 cases: 35 good and 50 bad.
- **The three first-version limitations are now rules,** so 7D needs no workaround:
  - subject-aware direction ("PIA was faster than LEC" is correct);
  - negation ("did not lose time");
  - corner counts tied to `corners_lost` / `corners_gained`.
- **Mutation kill rate is 99.1% (224/226).** The only survivors are two equivalent mutants
  (corners lost = corners gained = 7).
- **The 7A masking bug ("Turn 2" consuming the "2" of "Turn 23") is fixed** in `parse_numbers`,
  with a regression test.
- **For 7D's prompt:** use driver codes rather than pronouns, and one claim per sentence (the
  remaining limitations in `docs/validator_rules.md`).

## Phase 7C — Template report (P1, 2026-10-13)

- **Review (2026-10-13).** All points raised at the review were applied:
  - Segment bullets separate the metrics that explain the result from the ones that went the
    other way ("…, despite …").
  - The headline names the biggest sector in the lap's own direction.
  - Driving suggestions prefer different causes and vary the wording when a cause repeats.
- **Faster property test.** It draws one Hypothesis seed per FactSheet and generates values with
  `random.Random`. 1,000 examples now run in about 23 s instead of about 100 s, with the same
  distributions.
- **The validator masks system text quoted verbatim** (notes, the tyre "unavailable" reason).
  Exact copies of FactSheet text are grounded; everything else is still checked.

## Phase 8 — Storage: PostgreSQL + TimescaleDB (P1, 2026-10-14)

- **One contract, two stores.** `tests/test_repository_contract.py` runs the same 20 behaviour tests against `ParquetRepository` and `PostgresRepository`. They cover:
  - round trip, including NaN and exact floats;
  - manifest, filters, stored order;
  - absence and staleness;
  - idempotent re-ingest, overwrite, and a failed save leaving the old session intact;
  - schedule cache, key/driver validation.

  The Parquet-only layout and atomic-file tests stay in `test_ingestion_repository.py`.
- **Where the Postgres tests get a database.** From `RACEIQ_TEST_DATABASE_URL` (CI service container), else a throwaway `timescale/timescaledb:2.17.2-pg16` via testcontainers when Docker runs, else skipped with the reason. They never pass silently.
- **Schema** (`app/db/schema.py`, SQLAlchemy 2.0 Core). The child tables keep the full pydantic model as JSONB next to the columns used for queries. Round trips are exact, and the indexed columns (`laps (session_key, driver, lap_number)`, unique keys for idempotent ingest) serve queries.
- **Telemetry.** One row per sample in a hypertable:
  - `ts` = session start + lap start + sample time;
  - 30-day chunks;
  - compression segmented by `lap_id`, ordered by `sample_idx`, with a policy for chunks older than 90 days;
  - Timescale's steps run only when the extension exists, so plain Postgres works.
- **Saving.** A whole session is replaced in one transaction (delete + insert), so readers never see half a session. Telemetry is loaded with `COPY`.
- **Measured on the real `data/`.** 25 sessions (~20 M samples) migrate in 9.5 min (~35k samples/s, ~25 s per race), which is acceptable next to FastF1 download times. A full comparison takes 71 ms on Postgres vs 61 ms on Parquet, with an identical result.
- **Migrations.** Alembic, with the initial revision autogenerated against TimescaleDB and checked by `compare_metadata` in CI (schema.py and migrations must agree). Up → down → up is tested.
- **Switching.** `RACEIQ_STORAGE_BACKEND=parquet|postgres` (default parquet: the frictionless local/demo mode). `scripts/migrate_parquet_to_postgres.py` copies everything, idempotently (`--only-missing`).

## Phase 8 — Cache: Redis behind one protocol (P1, 2026-10-14)

- **`app/caching.py`.** One `Cache` protocol (bytes in/out, set-if-absent, compare-and-delete, prefix delete) with `MemoryCache` (process-local LRU + TTL, the default) and `RedisCache` (`RACEIQ_REDIS_URL`; shared by every worker). The comparison service, the engineer report cache and the FactSheet store all go through it. The per-session lap tables stay in a small in-process LRU.
- **Keys.** `raceiq:v1:{kind}:{scope}:{digest}`:

  | Kind | Contents | TTL |
  |---|---|---|
  | `cmp` | comparisons | 24 h |
  | `fs` | FactSheets by request | 24 h |
  | `fsh` | FactSheets by hash | 24 h |
  | `rpt` | engineer reports | 7 d |
  | `sched` | event lists | 6 h |

  Digests include every version that defines the value: storage and findings schema versions, the session's `ingested_at`, the FactSheet version, prompt version and model, and the template version. A re-ingest or schema bump can never serve stale data, and an ingest also deletes the session's prefixes.
- **Single flight.** `SET NX` lock with a TTL. Waiters poll for the value; a failed leader releases the lock and one waiter takes over; nobody waits forever. Tested: 16 concurrent identical requests and three "workers" sharing Redis each compute once.
- **Redis unavailable** → logged, and requests compute uncached (never fail because of the cache).
- **Engineer report fallbacks** are still never cached under the LLM key (`ttl_for` → 0).

## Phase 8 — Background jobs: RQ (P1, 2026-10-14)

- **RQ, not Arq or Celery.** Ingestion (FastF1) and training are synchronous, CPU- and IO-heavy Python. RQ runs plain sync functions with no event loop, needs only Redis (already there for the cache), and has delayed jobs (`enqueue_in`) for retry backoff. Celery is more machinery than two job kinds need.
- **The lifecycle is ours, not the queue's** (`app/jobs/runner.py`). RQ only executes `run_job(id)`; status, progress, attempts, retries and errors live in `ingest_jobs` (Postgres) or in memory, so `GET /jobs/{id}` works the same with any queue. Retries:
  - up to 3 attempts, with backoff 5 s → 20 s → 60 s;
  - permanent errors (a session that does not exist, invalid input) fail at once;
  - stored errors are the type and message only;
  - a second submit for an active target returns the running job.
- **Queues.**
  - `RQQueue` (Redis + the `worker` container, `python -m app.jobs.worker`; Linux/macOS, since RQ forks);
  - `ThreadQueue` (no Redis: jobs run in the API process, local dev only);
  - `InlineQueue` (tests).

  A real RQ round trip on Redis is tested with `SimpleWorker` + `TimerDeathPenalty`, so it also runs on Windows.
- **API.** `POST /sessions/{key}/ingest` → **202** + job (`Location: /api/v1/jobs/{id}`). The frontend polls every 1.5 s and shows real progress (percent and stage, e.g. "telemetry 200/500 laps"), a retry note, and the error with a "Try again" button.
- **Migration 0002** adds `params`, `result`, `max_attempts` and `run_after` to `ingest_jobs`, which also tests the migration chain (up/down across two revisions).

## Phase 8 — Object storage and the database model registry (P1, 2026-10-14)

- **`app/objectstore.py`.** `LocalObjectStore` (a directory, the default) and `S3ObjectStore` (boto3; any S3-compatible service). Keys are validated (no `..`, absolute paths, backslashes); every put returns the SHA-256.
- **Local S3 server: SeaweedFS** (`chrislusf/seaweedfs:4.48`, S3 gateway on :8333), not MinIO. MinIO no longer publishes pullable community images (Docker Hub: "pull access denied"; quay.io: 401), so docker-compose and the tests use SeaweedFS. The code is plain S3, so MinIO, AWS S3 or R2 work unchanged (`RACEIQ_S3_ENDPOINT_URL`).
- **`DbModelRegistry`.** `model_registry` rows (name, version, kind, status, `object_key`, `sha256`, metadata) with artifacts in object storage. It shares checksum-verified loading with the file registry. Retired versions are never served, and promoting a version to production demotes the previous one in the same transaction. Tested: same predictions as the file registry, tampered or missing artifacts refused.
- **Publishing and training.**
  - `scripts/publish_models.py` publishes the existing `models/registry.json` idempotently.
  - The training job (kind `train`, admin only) rebuilds the models with the 6E builder and publishes them.
  - `RACEIQ_MODEL_REGISTRY=file|db` (default `file`, so a checkout works with no services).

## Phase 8 — Authentication and public endpoints (P1, 2026-10-14)

- **Public stays public.** Comparisons, sessions, laps, tyres and seasons need no account, so the demo stays frictionless (the plan's pitfall). Accounts are needed for the AI engineer (LLM cost control) and saved comparisons. **Admin** is needed for ingest, training, job status and the engineer metrics.
- **Email + password in-house** (argon2id, PyJWT), not an external auth provider: no third-party keys, and it works offline and in CI. An OAuth provider can be added later behind the same `current_user` dependency.
- **Tokens.**
  - **Access:** a 15-minute Bearer JWT, kept in memory in the browser.
  - **Refresh:** a single-use, rotated, httpOnly / Secure / SameSite=Strict cookie scoped to `/api/v1/auth`, with reuse detection that revokes everything.
  - **Revocation:** `token_version` on the user.

  CSRF is handled by a custom header plus an Origin check on the two cookie endpoints. A refresh with no cookie returns 204 (signed out is not an error).
- **Rate limits** are Redis token buckets per IP (API, auth) and per user (engineer, admin) → 429 + `Retry-After`. They fail open. The e2e backend disables the per-IP limit (one page legitimately makes dozens of requests); unit tests cover it.
- **Details:** `docs/security.md`.

## Phase 9 — Delivery, deployment and observability (P1, 2026-10-15)

- **Render for the backend, Vercel for the frontend, Sentry for errors** (chosen 2026-10-15; the comparison is in `docs/deployment.md`).
  - **Why Render:** it covers a web service, a long-running worker, managed Postgres and Key Value, image-backed deploys by digest, and a pre-deploy migration hook in one Blueprint (`render.yaml`, schema-validated). Fly.io and AWS need much more assembly for the same thing.
  - **Trade-off:** no TimescaleDB, so `telemetry_samples` is a plain table there. The migration already handled that.
- **Build once, promote the digest.**
  - GitHub Actions builds `raceiq-backend` / `raceiq-worker` once per commit or tag, scans the pushed digest with Trivy, and every environment deploys that exact digest through Render's deploy hook (`imgURL`).
  - A rollback redeploys an existing digest and never rebuilds.
  - `ci.yml` became a reusable workflow, called first by `deploy.yml` on pushes, so CI runs once per push and only a green CI deploys.
- **Production approval** is GitHub's environment protection (required reviewers on `production`), not custom logic in the workflow.
- **Missing credentials don't break `main`.** The deploy preflight lists missing secrets ("API KEY NEEDED"). On `main` it warns and skips, because the hosting accounts are not set up yet; on a tag it fails.
- **Models baked into the image** through a named build context (`models=./models`, with an empty `FROM scratch AS models` fallback).
  - Render has no disk shared between the API and the worker, so production serves the file registry from the image, and a new model ships as a reviewed commit plus a deploy (with the ML regression gate).
  - The S3-backed registry stays available for in-app training once a bucket exists.
- **Same-origin frontend.** The browser calls `/api/*` on the Vercel origin, and Next.js rewrites to the environment's API (`BACKEND_URL`, also exposed as `NEXT_PUBLIC_API_URL`). That keeps the `SameSite=Strict` refresh cookie first-party; calling Render directly from the browser would make it a third-party cookie and break silent refresh.
- **Expand-only migrations,** so that a rollback is an image switch. `alembic downgrade` is reserved for local development.
- **Logs.**
  - structlog for everything, with the standard library routed through the same renderer; JSON in containers.
  - The request id is bound per request through contextvars.
  - Uvicorn's access log was replaced by an `http.request` line without client IPs.
  - Secret- and PII-named keys are redacted at render time.
- **Metrics.**
  - `prometheus_client` in multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`), because the API runs several uvicorn workers.
  - The RQ worker runs jobs in forked work-horses and is never scraped, so job metrics and the drift window go to Redis, and the API's `/metrics` reads them at scrape time.
  - Route labels are templates, rebuilt from the request path because FastAPI 0.143 keeps nested routers' routes without the parent prefix.
  - Known series are pre-created at 0: the end-to-end check showed that a counter born at 1 never trips `increase()`.
- **Model drift.** The production model scores each newly ingested race with the offline protocol (`simulate_race`, h5) and races in its training data are excluded. The alert compares the step-weighted rolling MAE over the last 10 races with the registry's recorded walk-forward MAE (+25%, ≥ 3 races).
- **Sentry without PII.** Backend: `send_default_pii=False`, plus a `before_send` that drops cookies, bodies and the user. Frontend (`@sentry/nextjs` 11): `dataCollection` off for user info, cookies, bodies and query strings. Ingest failures and LLM fallbacks are sent as grouped messages (fixed fingerprints), so Sentry alert rules can count them.
- **Backups.** Render's managed backups first, and `ops/db/backup.sh` (pg_dump custom format, optionally AES-256) as the portable copy. `ops/db/restore_drill.sh` proves restorability, including TimescaleDB's pre/post-restore steps, and CI runs it on every build against the seeded database.

## Frontend redesign: Senna (P1, 2026-10-15)

- **Name.** The web app is called **Senna**: wordmark, page title, footer notice and user-visible errors. The backend, the API contract (`X-RaceIQ-CSRF`, the `raceiq_refresh` cookie), the repository and the docs keep RaceIQ, so nothing server-side changed.
- **Layout.**
  - **Tabs.** Five tabs across the top (Pit Wall, Lap Comparison, Track Map, Tire Analysis, AI Engineer), shown as words. A red underline slides to the selected one. They form an ARIA tablist (arrow keys, Home/End).
  - **URL.** The tab is part of the shareable URL (`?tab=laps`; Pit Wall is the default and omitted).
  - **Removed.** The left sidebar, its session box and the light theme.
- **Engineer dock.** The right column (232 px, the old sidebar's width) holds the AI engineer on every tab.
  - "Generate report" shows the sections of the validated engineer report that the open tab is about (`TAB_FOCUS` in `lib/engineer.ts`; the Track Map report keeps only bullets tied to a corner, straight or sector). No new backend endpoint: it is the same cached, fact-checked report.
  - The chat lives in the dock; its suggested questions follow the tab, and one conversation per comparison survives tab switches.
  - The AI Engineer tab shows the full report plus a table of every fact it cites. Clicking a bullet there opens that corner on the Track Map tab.
- **Theme.** One dark theme, red (`#e10600` fills, `#ff3b2f` text and lines) on carbon black, typeset in Titillium Web, self-hosted through `@fontsource` (no build-time font download).
- **Motion, all CSS and all off under `prefers-reduced-motion`:**
  - **Tab switch:** a broadcast-style replay wipe (a translucent red band with speed streaks and a chequered edge) while the new tab launches in.
  - **KPI strip:** five start lights come on one by one, then go out as the lap times appear.
  - **Charts:** they reveal left to right behind a red timing beam.
  - **Track map:** the circuit outline draws itself like a lap being traced.
- **Bigger graphs and more detail per tab.**
  - **Lap Comparison:** telemetry 920 px tall, plus a speed-trap table per straight and a mini map that follows the chart cursor.
  - **Track Map:** a 680 px map and a table of every corner (Δ time, entry / apex / exit speed, braking, causes).
  - **Pit Wall:** a brief written from the verified findings, plus tyre, temperature and alignment context.
  - **Tire Analysis:** charts 460 / 380 px.

## No sign-in in the web app (P1, 2026-10-15)

- **The frontend has no accounts.** The sign-in dialog, account menu, saved comparisons and the admin "Ingest session" button are gone.
- **The AI engineer is public.** `/engineer/report`, `/ask` and `/facts` no longer require a token. LLM cost control is the per-IP bucket (20/min, Redis-shared), the `RaceIQLlmCostBudget` alert and the `ENGINEER_LLM_ENABLED` kill switch.
- **Admin stays protected.** Ingest, training, job status and the engineer metrics still need an admin token. Operators get one from `POST /api/v1/auth/login` (an address in `RACEIQ_ADMIN_EMAILS`) or use the CLI. A session that isn't stored shows the CLI command.
- **What remains in the backend.** The `/auth` and `/me` endpoints and the users table remain, used only by operators; saved analyses are unused by the UI.
