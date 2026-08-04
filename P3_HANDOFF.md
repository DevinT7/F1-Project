# P3 handoff — tire degradation + AI race engineer

For **P4** (dashboard) and anyone rebuilding the model. Covers the two endpoints P3
owns: what they return, what the units are, and what to do when a value is `null`.

Everything here is checked by tests in `backend/tests/` — if this doc and the code ever
disagree, the code is right and this doc is a bug.

## `GET /api/tires/degradation`

    /api/tires/degradation?session_key=9159&driver_number=55&horizon=10&stint_number=2

| param | default | notes |
|---|---|---|
| `session_key` | required | integer. The router types it `int`, like every other route |
| `driver_number` | required | |
| `horizon` | `10` | how many laps ahead to predict |
| `stint_number` | current/last stint | analyse an earlier stint instead |

Returns `TireAnalysis`. Errors are `502` when OpenF1 is unreachable; a driver with no
usable stint data is **not** an error — it's a populated-shape response with empty
`predictions` (see "the empty response" below).

### Fields and units

| field | unit | render as |
|---|---|---|
| `compound` | `"SOFT"` / `"MEDIUM"` / `"HARD"`, or `null` | badge |
| `tyre_age_laps` | **laps on this set of tyres**, count | "24 laps old" |
| `degradation_s_per_lap` | seconds per lap, on **fuel-corrected** lap time | "+0.06 s/lap" |
| `remaining_competitive_laps` | a **count of laps from now**, or `null` | "8 laps left" |
| `recommended_pit_window` | **RACE LAP NUMBERS**, a `(lo, hi)` pair, or `null` | "PIT WINDOW 24–27" |
| `model_confidence` | 0–1, capped at 0.97 | a bar, or a number to 2 dp |
| `predictions[]` | `laps_ahead`, `predicted_lap_time_s`, `lower_s`, `upper_s` | line + band chart |
| `model_name` | string, see below | small caption |

Two things worth pinning down, because they are the easiest to get wrong:

**The pit window is race lap numbers, not tyre age.** `(24, 27)` means "pit between lap
24 and lap 27 *of the race*". It is safe to render verbatim. `remaining_competitive_laps`
is the other kind of number — a count relative to now — so `24` there means "24 more
laps", not "lap 24". The decision is documented in `schemas.py` next to the field, and
`tests/test_phase1_tires.py::test_pit_window_is_race_lap_numbers_not_tyre_age` holds it
in place.

**Predicted lap times are raw wall-clock seconds.** The fit runs on fuel-corrected times
(otherwise fuel burn-off cancels degradation and the slope can come out negative), but
`predicted_lap_time_s` is un-corrected on the way out, so it is the number a stopwatch
would show. `degradation_s_per_lap` is the one figure that stays on the corrected scale —
it is a rate of tyre wear with fuel effect removed, which is the whole point of it.

### Nulls, and what they mean

- `recommended_pit_window: null` and `remaining_competitive_laps: null` — the fitted
  slope is ≤ 0. Real, and common early in a stint: the tyres are still warming up or the
  track is rubbering in. Render "no pit window yet", **not** "0" and not an error.
- `compound: null`, `tyre_age_laps: null`, `predictions: []`, `model_confidence: 0.1` —
  the empty response. Fewer than 3 clean laps in the stint, no stint data, or an unknown
  `stint_number`. Show "not enough data", not a zero-degradation reading.
- `degradation_s_per_lap` is never null; on the empty response it is `0.0`. Use
  `predictions == []` to detect "no data", not the degradation value.

### `model_name` values

| value | means |
|---|---|
| `baseline-linear` | per-stint straight-line fit. **This is what ships today** |
| `random_forest`, `lightgbm`, `gradient_boosting`, `xgboost`, `linear` | a trained artifact is being served |

The artifact is gitignored, so a fresh clone serves `baseline-linear` — by design, and
tested (`test_phase4_model_serving.py`). Even *with* `ml/models/tire_model.pkl` present
the baseline is served, because the artifact records `beats_baseline=False`: no ML
candidate beat the trivial baselines on walk-forward MAE (`ml/models/MODEL_CARD.md`).
`RACEIQ_TIRE_MODEL_FORCE=1` overrides that gate for demos.

P4 does not need to branch on any of this. **The response shape is identical in every
case** — treat `model_name` as a caption, not a switch.

## `POST /api/ai/report`

Body is `AIReportRequest`: `comparison` (P2's `LapComparison`), `tires` (the
`TireAnalysis` above), `question` — all optional. Supply whichever you have; the report
degrades gracefully with only one, and with neither it says it has nothing to work from
rather than inventing something.

Returns `AIReport`: `summary`, `insights[]`, `recommendations[]`, `strategy_options[]`,
`source`, `grounded`.

| `source` | means | show |
|---|---|---|
| `template` | no API key configured. The normal state; deterministic, always available | nothing special |
| `llm` | LLM output that **passed** the grounding check | optionally an "AI" badge |
| `template-fallback` | the LLM was tried and rejected — a bad key, a timeout, or it wrote a number that isn't in the computed findings | worth surfacing quietly; something is misconfigured |

`grounded` is `true` on every path that can reach P4. It is not a promise in a prompt:
after the LLM responds, every numeric token it wrote is checked back against the facts
dict within the rounding tolerance implied by how it was written, and one unverifiable
number discards the whole response
(`test_phase5_ai_engineer.py::test_fabricated_number_is_rejected_and_falls_back_to_template`).

`question` is answered from the computed findings — where time was lost, why slower in a
sector, whether the stint is still competitive, which corners to focus on, when to pit,
and driver-vs-tires-vs-traffic. A question the facts can't support gets an honest "I
can't answer that from what was computed" rather than a guess.

## Rebuilding the model from scratch

Full detail in `ml/README.md`; the short version:

    cd ml && pip install -r requirements.txt
    export RACEIQ_FASTF1_CACHE_DIR=~/.fastf1        # persistent, or the pull is slow twice
    python build_dataset.py --years 2023 2024 --plot   # → data/stints.parquet + DATASET_CARD.md
    python train.py                                    # → models/tire_model.pkl + MODEL_CARD.md

Both outputs are gitignored. `train.py` memoises walk-forward results per
(target, model); pass `--cache-clear` after changing features or params.

Point the backend at a different artifact with `RACEIQ_TIRE_MODEL_PATH`.

## Tests

    cd backend && pytest -q          # 130 tests, fully offline
    cd ml && python test_features.py && python test_train.py

Nothing in the suite touches the network. The one check that does is opt-in:

    cd backend && python scripts/validate_real_session.py

It runs the whole grid of the 2023 Singapore GP through the live path, range-checks the
degradation figures and pit windows, prints each driver's **actual** pit laps beside the
prediction, and times a cold sweep against a warm one to prove the caches work. Run it
before a demo — offline tests prove the maths is self-consistent, not that it's right.

## If something looks wrong

| symptom | first thing to check |
|---|---|
| pit window is a small number like `(4, 8)` mid-race | it's being derived from tyre age somewhere — that's the Phase 0 unit bug coming back |
| degradation is negative | fuel correction. `FUEL_EFFECT_S_PER_LAP` must be the same value in `tires.py` and `ml/build_dataset.py`; a test asserts it |
| confidence is 0.97 on a 3-lap stint | `_confidence` ignoring sample count |
| `model_name` unexpectedly not `baseline-linear` | someone set `RACEIQ_TIRE_MODEL_FORCE=1`, or retrained a winner |
| AI report has a number that isn't in the findings | shouldn't be possible on any path; if it is, the grounding check has a hole — file it, it's the one guarantee this layer makes |
