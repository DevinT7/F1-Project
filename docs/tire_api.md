# Tyre prediction API (Phase 6E)

Code:
- `backend/app/tires/pit_window.py`: the pure pit-window function.
- `registry.py`: the model registry.
- `serving.py`: the servable models.
- `service.py`: the prediction service.
- `backend/app/api/routes/tires.py` and `schemas/tires.py`: the endpoints.

Build script: `backend/scripts/build_tire_registry.py`. Tests: `tests/test_tires_pit_window.py` and
`tests/test_tires_service.py`.

## Endpoints

### `GET /api/v1/tires/predict?session=&driver=&lap=&horizon=[&threshold=]`

| Parameter | Meaning |
|---|---|
| `session` | race session key, e.g. `2024-14-R` (`_` also accepted) |
| `driver` | driver code (`LEC`) or number (`16`) |
| `lap` | the current lap. **Only laps ≤ `lap` are used** |
| `horizon` | laps to forecast, 1–25 (default 5) |
| `threshold` | optional pit-window X in seconds, 0–5 (default 1.0) |

Response (`TirePrediction`):

```json
{ "status": "ok", "reason": null, "session_key": "2024-14-R", "driver": "LEC", "lap": 20, "horizon": 3,
  "stint": 2, "compound": "HARD", "tyre_age": 8, "clean_laps_used": 6,
  "degradation_s_per_lap": 0.0338, "degradation_ci": [0.0289, 0.0387],
  "predicted_laps": [{"lap": 21, "pred_s": 108.045, "lo_s": 107.198, "hi_s": 108.892}, "…"],
  "pit_window": null, "pit_window_reason": "forecast_uncertainty_too_high",
  "remaining_competitive_laps": null,
  "model": {"name": "baseline_b_conformal", "version": "2024-07-28.6681d16a", "trained_through": "2024-07-28"},
  "warnings": ["pit_window: forecast_uncertainty_too_high (interval width 4.37 s at lap 44 exceeds 2 × 1 s)"] }
```

When the window is computed, `pit_window` looks like this:

```json
{"earliest_lap": 44, "central_lap": null, "latest_lap": null, "threshold_s": 1.0,
 "threshold_pace_s": 107.183, "best_pace_s": 106.183, "pace_basis": "fuel_adjusted_model_term",
 "method": "interval_crossing_v1", "confidence": "low",
 "assumptions": ["ignores undercut and overcut", "ignores traffic", "ignores track position",
                 "ignores safety-car probability", "ignores tyre availability (sets left)",
                 "threshold is a heuristic (best pace + X seconds), not a team rule"],
 "notes": ["no crossing within horizon"]}
```

The schema is extended in three ways beyond the plan's example: `reason`, `pit_window_reason`, and
a few extra pit-window fields (`threshold_pace_s`, `best_pace_s`, `pace_basis`, `notes`). Without
them, a null value or a pace number couldn't be explained.

### `GET /api/v1/tires/models`

Lists every registered version with its status, dataset hash, git SHA, trained-through date,
interval α, and the 6D evaluation of that configuration (holdout once, walk-forward on the
development races).

### Errors

All errors use the standard `{"error": {code, message, status_code, request_id}}` envelope.

| Status | Code | When |
|---|---|---|
| 404 | `session_not_found`, `driver_not_found`, `lap_not_found` | unknown session, driver or lap |
| 422 | `not_a_race_session` | "tire forecasting requires a race session" (Q, FP, sprint…) |
| 422 | `validation_error` | horizon outside 1–25, malformed key, … |
| 503 | `model_not_available` | no registry, no production entry, or a missing artifact |
| 503 | `model_checksum_mismatch` | an artifact's SHA-256 differs from the registry |

`status: "insufficient_data"` is **not** an error (HTTP 200). It carries a `reason` and no
predictions, window or degradation. Causes:

- fewer than 5 clean laps in the stint so far (configurable as `TireServiceConfig.min_clean_laps`)
- stint, compound or tyre age missing at that lap
- wet compounds
- a compound the model has no estimate for
- the last lap of the race

## How a prediction is made

1. **The cut.** Take every driver's laps with `lap_number ≤ lap` (the exclusion rules need the
   whole field: neutralizations and red-flag restarts). Apply the 6A exclusion rules to that cut,
   keeping short stints so the clean-lap count can be reported, and build 6A rows. Nothing after
   `lap` is read. A test rewrites every later lap of every driver (lap times, compounds, stints,
   a safety car, rain, temperatures, pit stops) and the response is identical. It also checks that
   changing the history does change the response, so the test can't pass vacuously.
2. **History:** the driver's clean laps in the stint in progress at `lap`. Require at least 5.
3. **Future laps:** lap+1 … min(lap+25, final lap), with only known quantities. Tyre age goes
   +1 per lap, `fuel_proxy` = scheduled laps − lap, and track temperature is frozen at the cut.
   `predicted_laps` returns the first `horizon` of these. The pit window uses all of them.
4. **Forecast.** The production model gives the point forecast. The 80% split-conformal interval
   adds ± the calibrated quantile for that horizon step, calibrated per step for steps 1–25. A
   step without calibration data would reuse the largest quantile and say so in `warnings`.
5. **Degradation:** Baseline B's per-compound β_age with its 95% CI (cluster-robust or
   conventional SE, the larger of the two; t with G−1 degrees of freedom). It is a combined
   adjusted trend (wear plus residual track evolution after the fuel adjustment), not pure wear. A
   model without a degradation term (persistence) returns null with a warning.

## Pit window: `interval_crossing_v1`

A pure function, unit-tested on hand-computed curves (`pit_window.interval_crossing`).

- **best_pace:** the median of the 3 fastest clean laps of the stint so far. With a fuel term in
  the model, best pace and all curves are fuel-adjusted (lap time − β_fuel · fuel_proxy) and
  `pace_basis = "fuel_adjusted_model_term"`. Otherwise they are raw (`"raw"`).
- **threshold:** best_pace + X, with X = 1.0 s by default. This is **a heuristic, not a team
  rule**, and the UI must label it so.
- **Crossings:**
  - `earliest_lap` is the first lap where the pessimistic curve (hi) ≥ threshold.
  - `central_lap` is the first lap where the forecast (m) ≥ threshold.
  - `latest_lap` is the first lap where the optimistic curve (lo) ≥ threshold.

  lo ≤ m ≤ hi is enforced, so earliest ≤ central ≤ latest (property-tested). A curve that never
  crosses gives null plus the note "no crossing within horizon", never an invented lap. Laps are
  capped at the final race lap.
- **Confidence:**
  - high: window ≤ 3 laps and ≥ 10 clean laps
  - medium: window ≤ 6 laps
  - low: otherwise, including an open-ended window
- **Null window,** with `pit_window_reason: "forecast_uncertainty_too_high"`, when either:
  - the interval width at the end of the horizon is > 2·X, or
  - the window is wider than 10 laps.
- `remaining_competitive_laps` = central_lap − lap, when central_lap exists.
- **Assumptions (always attached):** ignores undercut/overcut, traffic, track position,
  safety-car probability and tyre availability.

Never present one pit lap as the answer. The window and its assumptions go together.

### Known consequence: the window is usually null

The plan's null rule (interval width at the end of the horizon > 2·X) meets per-lap conformal
intervals. The production model's 80% half-width grows from about 0.85 s at step 1 to about
2.1 s at step 25. That is lap-to-lap noise (traffic, lifts, dirty air), not uncertainty about
the tyre trend. A 25-lap horizon is therefore about 4.2 s wide, more than 2 × 1 s.

Measured on all 24 races of the 6A dataset: every driver, every 4th lap from lap 6, 6,261
requests. 1,585 of them (25%) are `insufficient_data` (under 5 clean laps or a fresh stint).
Of the 4,676 forecasts:

| X | Window returned | …with > 5 laps left | `central_lap` defined |
|---|---|---|---|
| 1.0 s (default) | 230 (4.9%) | 0 | 154 |
| 1.5 s | 971 (20.8%) | 476 | 247 |
| 2.0 s | 1,811 (38.7%) | 1,316 | 168 |

**With the default X, the window only appears when the horizon is shortened by the end of the
race.** The behaviour follows the plan (null rather than a fabricated window), but it is not yet
useful in a race. This is a decision for review. The options, none applied:

1. Judge the 2·X rule at the crossing lap rather than at the end of the 25-lap horizon.
2. Build the window from an interval on the **pace trend** (for example, a rolling 3-lap mean)
   rather than single-lap intervals.
3. Raise the default X, or shorten the pit-window horizon.

`central_lap` stays rare even at larger X. The fuel-adjusted Baseline B trend climbs only about
0.03–0.06 s per lap, so it seldom gains X seconds within 25 laps.


## Model registry

- **Registry file:** `models/registry.json`, committed. Phase 8 moves it to a DB or object
  storage.
- **Entry fields:** `name, version, kind, status (production | candidate), artifact + sha256,
  calibration_residuals + calibration_sha256, dataset_hash, git_sha, trained_through (+ session),
  alpha, metrics, notes`. Paths are relative to the registry, and a path escaping its directory
  is refused.
- **Artifacts are data, not code:** `model.json` holds the coefficients and `residuals.npz` the
  per-step absolute calibration residuals (loaded with `allow_pickle=False`). This replaces the
  6D pickle of Baseline B: 119 MB of statsmodels internals that could only be unpickled from
  inside a function. The two served models together take about 290 KB.
- **Loading:**
  - It is lazy, on the first prediction.
  - It verifies both checksums. A mismatch raises `ChecksumMismatchError` (503).
  - The loaded model is cached in memory by (version, checksums).
  - Exactly one entry may be `production`.
- **Rebuild:** `cd backend && python -m scripts.build_tire_registry [--production persistence]`.
  It refuses a dirty tree, refits on the full 6A dataset, and writes new checksums.

### Registered models

| Name | Kind | Status | 6D holdout h=5 MAE | 6D holdout 80% coverage | WF h=5 coverage |
|---|---|---|---|---|---|
| `baseline_b_conformal` | Baseline B + split-conformal 80% | **production** (6D protocol fallback) | 0.505 s | 91.5% | 77.7% |
| `persistence_conformal` | persistence + split-conformal 80% | candidate (6D report recommendation) | 0.372 s | 87.4% | 76.7% |

The 6D gate decides which model is production. Switching is
`build_tire_registry --production persistence`, or editing the two `status` fields.

- **Calibration:** both models fit their point model on the earliest 70% of the 24 races and
  calibrate on the most recent 30%, with forecast horizons 5, 10 and 25. `trained_through`
  2024-07-28 is the date of the last calibration race (Spa 2024).

## Caching and performance

- **Response cache:** an in-process LRU keyed by (session, driver, lap, horizon, threshold,
  model version, artifact checksum). A new model version can never be served a stale answer.
  Session lap tables and loaded models are cached too. Phase 8 moves this to Redis.
- **Measured locally:** real Spa 2024 data, 96 distinct requests over 8 drivers and 12 laps,
  with the model and session already loaded:
  - uncached: median 81 ms, **p95 127 ms**
  - cached: median 7 ms, **p95 12 ms**

  The target is p95 < 400 ms.

## Hindsight bias

Hindsight bias is the pitfall this API exists to avoid: forecasting mid-stint with the whole
stint in hand. The cut rule is enforced in one place (`TireService._predict`: `laps ≤ lap`), and
the cut-rule test guards it. The pit-window function never sees lap times at all, only curves.
