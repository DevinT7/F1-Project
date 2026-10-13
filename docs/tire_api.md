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

Response (`TirePrediction`), a real output (Spa 2024, Leclerc, lap 39, production model):

```json
{ "status": "ok", "reason": null, "session_key": "2024-14-R", "driver": "LEC", "lap": 39, "horizon": 3,
  "stint": 3, "compound": "HARD", "tyre_age": 14, "clean_laps_used": 11,
  "degradation_s_per_lap": 0.0338, "degradation_ci": [0.0289, 0.0387],
  "predicted_laps": [{"lap": 40, "pred_s": 107.392, "lo_s": 106.838, "hi_s": 107.947},
                     {"lap": 41, "pred_s": 107.373, "lo_s": 106.735, "hi_s": 108.01},
                     {"lap": 42, "pred_s": 107.353, "lo_s": 106.64, "hi_s": 108.065}],
  "pit_window": {"earliest_lap": 40, "central_lap": 41, "latest_lap": null, "threshold_s": 1.0,
                 "threshold_pace_s": 107.183, "best_pace_s": 106.183, "pace_basis": "fuel_adjusted_model_term",
                 "method": "interval_crossing_v2", "confidence": "low",
                 "assumptions": ["ignores undercut and overcut", "ignores traffic", "ignores track position",
                                 "ignores safety-car probability", "ignores tyre availability (sets left)",
                                 "threshold is a heuristic (best pace + X seconds), not a team rule"],
                 "notes": ["no crossing within horizon"]},
  "pit_window_reason": null, "remaining_competitive_laps": 2,
  "model": {"name": "baseline_b_recent_conformal", "version": "2024-07-28.6681d16a", "trained_through": "2024-07-28"},
  "warnings": [] }
```

Five laps earlier (lap 34) the forecast never reaches the threshold before the flag, and the
interval at the last lap is too wide, so `pit_window` is null and the reason is given:
`"pit_window_reason": "forecast_uncertainty_too_high"`, with the warning "interval width 2.06 s at
lap 44 exceeds 2 × 1 s".

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

## Pit window: `interval_crossing_v2`

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
  - the interval width **at the decision lap** is > 2·X. The decision lap is the central
    crossing, or the end of the horizon when the forecast never crosses. This is the v2 change;
    v1 always judged the width 25 laps out (see below). Or:
  - the window is wider than 10 laps.
- **Already past:** when the recent pace (mean of the last 3 clean laps, same basis) is already
  at or above the threshold, the window says so in `notes` ("threshold already reached before the
  current lap") instead of looking like a fresh "pit next lap" call.
- `remaining_competitive_laps` = central_lap − lap, when central_lap exists.
- **Assumptions (always attached):** ignores undercut/overcut, traffic, track position,
  safety-car probability and tyre availability.

Never present one pit lap as the answer. The window and its assumptions go together.

### Why v2: the v1 window was almost always null

As first specified (v1), the 2·X rule judged the interval at the end of a fixed 25-lap horizon,
even when the forecast crossed the threshold much sooner. With the 6D Baseline B, the 80%
half-width 25 laps out is about 2.1 s, so the window was null in 95% of real-race forecasts at
X = 1 s, and it never appeared with more than 5 laps left. Smoothing the pace (a 5-lap mean) does
not fix this: its step-25 half-width is still 1.57 s. The uncertainty is genuine drift in a
stint's pace over 25 laps, not lap-to-lap noise. So v2 keeps the 80% intervals and the 2·X
rule, and judges them where the decision is made.

Measured on all 24 races of the 6A dataset: every driver, every 4th lap from lap 6, 4,676
forecasts with status `ok`, X = 1.0 s.

| Rule and model | Window returned | …with > 5 laps left | `central_lap` defined |
|---|---|---|---|
| v1, 6D Baseline B | 230 (4.9%) | 0 | 154 |
| v1, recent-anchored Baseline B | 920 (19.7%) | 425 | 635 |
| **v2, recent-anchored Baseline B (production)** | **2,870 (61.4%)** | **2,375** | **2,585** |

Most v2 windows are open-ended (`latest_lap` null, confidence low). The pessimistic curve
crosses within a lap or two, the forecast crosses at `central_lap`, and the optimistic curve
doesn't cross within the horizon. That is the honest shape of an 80% interval of about ±0.8 s
around a trend of a few hundredths of a second per lap. `central_lap` is the number to show,
always with the window and its assumptions. `TireServiceConfig(pit_window_gate="horizon")`
restores v1.

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

| Name | Kind | Status | Dev LORO h=5 MAE | Dev WF 80% coverage (h=5) | 6D holdout h=5 MAE |
|---|---|---|---|---|---|
| `baseline_b_recent_conformal` | Baseline B anchored on the last 3 laps + split-conformal 80% | **production** | 0.373 s | 77.4% | not evaluated (holdout used once in 6D) |
| `persistence_conformal` | persistence + split-conformal 80% | candidate | 0.369 s | 76.7% | 0.372 s |
| `baseline_b_conformal` | 6D Baseline B (level from all history) + split-conformal 80% | candidate | 0.505 s | 77.7% | 0.505 s |

**Why the recent-anchored model is production (6E fix, after the 6D review).**
- **Accuracy:** on the 21 development races it is as accurate as persistence (LORO h=5 0.372 vs
  0.369 ± 0.02; h=10 0.439 vs 0.440). MLflow run `a48bd9fa3749428499c600247af4bb43`.
- **What persistence lacks:** it has a fuel term, a tyre-age trend and a degradation estimate.
  Persistence's forecast is flat, so it cannot drive a pit window or report degradation.
- **Its anchor:** it is Baseline B with persistence's anchor. 6C traced B's error to its
  all-history level.
- **Caveat:** its bias (−0.054 s) is marginally outside the 0.05 s bar. Like every model here,
  it does not meet the 6D bar.
- **No holdout number:** it was designed after the 6D holdout had been used, so no clean holdout
  estimate exists for it.

To switch: `python -m scripts.build_tire_registry --production persistence` (or `pooled_fixed_effects`).

- **Calibration:** every model fits its point model on the earliest 70% of the 24 races and
  calibrates on the most recent 30%, with forecast horizons 5, 10 and 25. `trained_through`
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
