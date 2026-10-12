# Tire evaluation harness and benchmarks (Phase 6C)

Code: `backend/app/tires/evaluation.py` (splits, forecast simulation, metrics, aggregation),
`models/persistence.py`, `models/forecasting.py` (the forecasting interface, plus Baselines A and B as
forecasters), and `backend/scripts/run_eval.py` (benchmark run with MLflow logging). Tests:
`tests/test_tires_evaluation.py`.

## What is measured

- **Splits, never random**, both produced by a single `split_generator`:
  - **Leave-one-race-out:** every race is tested once, trained on all the others.
  - **Walk-forward:** races are ordered by (year, round), the calendar order. Each race after
    the first 8 is tested with every earlier race as training data.
- **Forecast simulation:** this is the product's task. Each test stint with ≥ 12 clean laps is
  cut at k = 6, 8, …, n − h. The model sees the first k clean laps and predicts the next
  h ∈ {5, 10}.
- **Information boundary:** `forecast_inputs` is the only way a model sees the future laps.
  - **Masked:** lap times and `prev_lap_time_s`.
  - **Frozen at lap k:** temperatures, rainfall and position.
  - **Assumed:** `laps_since_neutralization` assumes no new safety car.
  - **Passed through:** only deterministic columns (lap number, tyre age, compound, stint, and
    `fuel_proxy` from the known race length).

  A test mutates every lap after k and asserts that predictions do not change, for all three
  benchmarks. It also checks that changing the history does change them, so the test can't
  pass vacuously.
- **No leakage through coefficients:** every fuel and degradation coefficient is fitted on the
  training races only. (In 6B, A borrowed β_fuel from a same-race fit. In forecasting that would
  have leaked the test race's future.)
- **Metrics:** MAE, RMSE and bias (predicted − actual) per horizon step. For models with
  intervals: coverage, mean width and the Gneiting–Raftery interval score. Also a slope check
  against each long stint's own ex-post OLS slope, which is **a reference, not ground truth**.
- **Aggregation:** every number is the **mean over races** with its standard error across races,
  never a pooled mean over laps, so long races do not dominate. A test pins this down on a
  deliberately unbalanced case.

## Benchmarks

<!-- benchmarks:start -->
<!-- benchmarks:end -->
