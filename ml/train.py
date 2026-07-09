"""
Train + compare tire-degradation models with walk-forward validation (Person 3).

    python train.py --data data/stints.parquet --out models/tire_model.pkl

Compares LinearRegression / RandomForest / GradientBoosting (+ XGBoost/LightGBM if
installed) predicting fuel-corrected lap time. Reports MAE + RMSE, saves the best model.
"""
from __future__ import annotations

import argparse
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

NUM = ["tyre_age", "lap_number", "track_temp", "air_temp", "stint"]
CAT = ["compound", "circuit", "driver", "team"]
TARGET = "fuel_corrected_s"


def _models() -> dict:
    m = {
        "linear": LinearRegression(),
        "random_forest": RandomForestRegressor(n_estimators=300, n_jobs=-1, random_state=0),
        "gradient_boosting": GradientBoostingRegressor(random_state=0),
    }
    try:
        from xgboost import XGBRegressor
        m["xgboost"] = XGBRegressor(n_estimators=400, learning_rate=0.05,
                                    max_depth=5, n_jobs=-1, random_state=0)
    except ImportError:
        pass
    try:
        from lightgbm import LGBMRegressor
        m["lightgbm"] = LGBMRegressor(n_estimators=500, learning_rate=0.05, random_state=0)
    except ImportError:
        pass
    return m


def _pipeline(model) -> Pipeline:
    pre = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT),
        ("num", "passthrough", NUM),
    ])
    return Pipeline([("pre", pre), ("model", model)])


def walk_forward(df: pd.DataFrame, model) -> tuple[float, float]:
    """Race-by-race: train on earlier races, test on the next. No leakage."""
    df = df.sort_values(["year", "circuit"]).reset_index(drop=True)
    keys = df[["year", "circuit"]].drop_duplicates().values.tolist()
    maes, rmses = [], []
    for i in range(3, len(keys)):  # need some history first
        test_key = keys[i]
        train = df[df.set_index(["year", "circuit"]).index.isin(
            [tuple(k) for k in keys[:i]])]
        test = df[(df["year"] == test_key[0]) & (df["circuit"] == test_key[1])]
        if len(train) < 100 or test.empty:
            continue
        pipe = _pipeline(model)
        pipe.fit(train[NUM + CAT], train[TARGET])
        pred = pipe.predict(test[NUM + CAT])
        maes.append(mean_absolute_error(test[TARGET], pred))
        rmses.append(np.sqrt(mean_squared_error(test[TARGET], pred)))
    return (float(np.mean(maes)) if maes else float("nan"),
            float(np.mean(rmses)) if rmses else float("nan"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/stints.parquet")
    ap.add_argument("--out", default="models/tire_model.pkl")
    args = ap.parse_args()

    df = pd.read_parquet(args.data).dropna(subset=[TARGET] + NUM)
    print(f"Loaded {len(df)} rows")

    results = {}
    for name, model in _models().items():
        mae, rmse = walk_forward(df, model)
        results[name] = (mae, rmse)
        print(f"{name:18s}  MAE={mae:.3f}s  RMSE={rmse:.3f}s")

    best = min(results, key=lambda k: results[k][0])
    print(f"\nBest: {best} (MAE={results[best][0]:.3f}s)")

    final = _pipeline(_models()[best]).fit(df[NUM + CAT], df[TARGET])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    joblib.dump({"pipeline": final, "features": NUM + CAT, "metrics": results[best],
                 "model_name": best}, args.out)
    print(f"Saved -> {args.out}")


if __name__ == "__main__":
    main()
