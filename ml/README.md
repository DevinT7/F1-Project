# RaceIQ — ML (Tire Degradation)  · Person 3

Trains the tire-degradation model behind `backend/app/services/tires.py`. The API works
with or without a trained model — same `TireAnalysis` shape either way — and as of
Phase 3 **the baseline is still what should be served**. See "Verdict" below.

## Pipeline

1. `python build_dataset.py --years 2023 2024 --plot` → `data/stints.parquet`
   - **FastF1** race laps + stints; drops pit / non-green / invalid / wet laps,
     fuel-corrects lap times, joins **per-lap** weather, circuit and a traffic proxy.
   - Also writes `data/DATASET_CARD.md` (provenance, filters, per-compound degradation
     slopes) and prints a sanity report. **Every compound's slope must be positive** —
     if one isn't, the fuel correction or the filtering is wrong and no model trained on
     that file is worth serving.
   - Flags: `--session Q` (default `R`), `--limit-rounds 3` for quick iteration,
     `--plot`, `--out`.
   - Point `RACEIQ_FASTF1_CACHE_DIR` at a persistent folder: the first pull of a season
     is slow, the second should be fast.
2. `python train.py` → compares five candidates against two trivial baselines with
   chronological walk-forward validation, saves `models/tire_model.pkl` and writes
   `models/MODEL_CARD.md` with every number below.
   - Flags: `--limit-models lightgbm linear`, `--max-train-races 0` (expanding window
     instead of rolling), `--cache-clear` (**do this after changing features or model
     params** — walk-forward results are memoised per (target, model) so a re-run only
     redoes what changed).
   - Full sweep is a couple of minutes; a cached re-run is seconds.
3. Phase 4 wires the artifact into `tires.py`, with a fallback to `_baseline_fit`
   whenever the file is missing, unreadable, or trained on a different feature list.

Offline checks, no FastF1 and no network:

    python test_features.py     # feature engineering, against a planted slope
    python test_train.py        # the validation harness itself

The one check that *does* need the network, opt-in and not part of `pytest` — runs the
served path over a real race and range-checks the output against what actually happened:

    cd ../backend && python scripts/validate_real_session.py

What P4 renders, and what every field means, is in `../P3_HANDOFF.md`.

## Target

**Shipped target is `delta`** — lap time in seconds *above the stint's own reference
pace*, where the reference is the median of that stint's first 5 clean fuel-corrected
laps. `tires.py` adds its own reference back to get wall-clock seconds; the slope across
a horizon, which is what the dashboard actually shows, is unaffected by the anchor.

The dataset's `fuel_corrected_s` (absolute lap time) was tried first and **does not
work**, in two different ways:

- *with* `previous_lap_pace` in the features, every model degenerates into persistence.
  It copies the previous lap, scores a flattering MAE, and its implied degradation is
  ~0.006 s/lap against the ~0.040 s/lap that is actually in the data.
- *without* it, models fingerprint each race through the continuous weather columns —
  every race has a near-unique track/air-temp trace — fit the training races to ~0.4 s
  and land 2–3 s out on a race they have never seen.

Removing the stint's pace level kills both: what is left to learn is the degradation
curve, which transfers across races.

`fuel_corrected_s` itself is lap time corrected to an end-of-race fuel load. Its
`FUEL_EFFECT_S_PER_LAP` constant is shared with `backend/app/services/tires.py`, and
`backend/tests/test_phase1_tires.py` asserts the two stay in sync.

## Features

- **Numeric (shipped model):** `tyre_age`, `stint`, `track_temp`, `air_temp`,
  `laps_since_safety_car`, `gap_ahead_s`.
- **Categorical:** `compound`, `circuit`, `driver`, `team`.
- `lap_number` and `previous_lap_pace` are in the dataset and in the absolute-target
  comparison, but **not** in the shipped model: `previous_lap_pace` collapses it into
  persistence and cannot be fed forward across a horizon without recursion, and
  `lap_number` mostly invites memorising one race's pace profile.
- `gap_ahead_s` is a traffic proxy (lap-start delta to the car ahead), sentinel `99.0`
  when the car leads the lap.
- `round` is carried through so folds sort **chronologically** rather than by circuit
  name.

## Evaluation

Walk-forward, one fold per race in `(year, round)` order, training on the 12 races
immediately before each test race. Scored only on laps past the first 5 of a stint, so
the per-stint baseline has a fit to extrapolate from and every candidate is judged on
identical rows. 40 folds over 2023–24.

Two baselines, both scored the same way:

| baseline | what it is | MAE |
|---|---|---|
| `persistence` | this lap matches the last one | **0.391 s** |
| `stint_linear` | the expanding-window straight line `tires.py` ships today | **0.458 s** |

Candidates on the shipped `delta` target:

| model | MAE (s) | RMSE (s) | MAE sd | implied s/lap |
|---|---|---|---|---|
| random_forest | **0.772** | 0.982 | 0.453 | +0.0241 |
| lightgbm | 0.841 | 1.058 | 0.538 | +0.0243 |
| gradient_boosting | 0.844 | 1.060 | 0.552 | +0.0162 |
| xgboost | 0.851 | 1.072 | 0.525 | +0.0235 |
| linear | 0.906 | 1.113 | 0.420 | +0.0296 |
| *(slope measured in the data)* | | | | +0.0397 |

"Implied s/lap" re-predicts the same laps with `tyre_age` pushed out 10 laps and
everything else held fixed. It matters more than MAE — it is what
`degradation_s_per_lap` is derived from, and a model can score well while being
completely flat in tyre age. Every candidate gets the sign and the order of magnitude
right and lands short of the measured slope.

Top SHAP features on the winner: `circuit` 0.160, `tyre_age` 0.153,
`laps_since_safety_car` 0.063, `gap_ahead_s` 0.054. `driver` and `team` are near the
bottom, so this is not a circuit-lookup table — but `compound` ranks *last*, which is
wrong, since `DATASET_CARD.md` measures SOFT degrading about twice as fast as HARD.

## Verdict (Phase 3)

**No ML candidate beats the trivial baselines**, so the artifact ships with
`beats_baseline=False` and `tires.py` should keep serving `baseline-linear`. The
artifact is saved anyway so Phase 4 can build and test the loading and fallback paths
end to end.

Why the baselines win: they read the stint in front of them, while a cross-race model
has to infer that stint's condition from circuit, car and weather. On a one-lap horizon
that is close to decisive. What would change the answer:

- Score a **multi-lap horizon** (5–10 laps out), which is what the pit-window decision
  actually needs. Persistence degrades badly there; a degradation model should not.
- Use the model for the **shape** and the stint's own laps for the **level** — a hybrid
  rather than a straight swap.
- More seasons, and a compound feature the model actually uses.

## Install

    pip install -r requirements.txt

`xgboost` is pinned to 2.1.4: 2.1.3 has no `__sklearn_tags__` and any Pipeline
containing it raises `AttributeError` under scikit-learn 1.6.

## Artifact contents

`models/tire_model.pkl` is a dict, not a bare pipeline:

| key | use |
|---|---|
| `pipeline` | fitted sklearn Pipeline (one-hot + model) |
| `model_name` | goes straight into `TireAnalysis.model_name` |
| `target`, `target_doc`, `ref_laps` | how to un-anchor a prediction into wall-clock seconds |
| `numeric_features`, `categorical_features`, `features` | column order to build a request row; check before trusting a `.pkl` |
| `metrics`, `baseline_mae`, `beats_baseline` | per-fold and mean MAE/RMSE, and the verdict flag |
| `residual_quantiles` | held-out error, for a confidence band that means something |
| `implied_degradation_s_per_lap` | sanity check against the measured slope |
| `feature_importances`, `dataset`, `trained_on` | provenance |
