# RaceIQ — ML (Tire Degradation)  · Person 3

Trains the tire-degradation model that powers `backend/app/services/tires.py`. Until a
trained model exists, the backend uses the baseline linear fit — the API works either
way (same `TireAnalysis` shape).

## Pipeline
1. `python build_dataset.py --years 2023 2024` → `data/stints.parquet`
   - Uses **FastF1** to pull race laps + stints, removes pit/SC/invalid laps,
     fuel-corrects lap times, joins weather + circuit.
2. `python train.py` → compares LinearRegression / RandomForest / XGBoost / LightGBM,
   logs MAE + RMSE with **walk-forward (race-by-race)** validation, saves the winner to
   `models/tire_model.pkl`.
3. Point the backend at it: load `models/tire_model.pkl` inside `tires.py` and replace
   `_baseline_fit`.

## Target & features (from the project doc)
- **Target:** fuel-corrected lap time (or degradation delta vs stint-start pace).
- **Features:** tyre_age, compound, circuit, driver, team, track_temp, air_temp,
  stint_number, previous_lap_pace, laps_since_safety_car, traffic.

## Evaluation
- MAE / RMSE in seconds.
- Walk-forward: train on earlier races, test on later ones (no leakage).
- SHAP for explainability (optional, matches doc's AI-layer transparency goal).

## Install
    pip install -r requirements.txt
