"""
Build the tire-degradation training set from historical races via FastF1 (Person 3).

    python build_dataset.py --years 2023 2024 --out data/stints.parquet

Removes pit-out/-in, safety-car and clearly invalid laps, fuel-corrects lap times, and
attaches per-lap tyre age, compound, weather and circuit. One row per clean race lap.
"""
from __future__ import annotations

import argparse
import os

import fastf1
import numpy as np
import pandas as pd

CACHE_DIR = os.environ.get("RACEIQ_FASTF1_CACHE_DIR", ".fastf1_cache")

# Rough fuel-effect correction: cars get ~0.03s/lap faster as fuel burns off.
# Correcting to a common fuel load isolates tyre degradation from fuel.
FUEL_EFFECT_S_PER_LAP = 0.03


def build(years: list[int]) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    fastf1.Cache.enable_cache(CACHE_DIR)
    rows: list[pd.DataFrame] = []

    for year in years:
        schedule = fastf1.get_event_schedule(year)
        for _, event in schedule.iterrows():
            rnd = event["RoundNumber"]
            if rnd == 0:
                continue  # testing
            try:
                ses = fastf1.get_session(year, rnd, "R")
                ses.load(telemetry=False, weather=True, laps=True)
            except Exception as e:  # noqa: BLE001
                print(f"skip {year} R{rnd}: {e}")
                continue
            df = _laps_to_rows(ses, year, event["EventName"])
            if df is not None and len(df):
                rows.append(df)
                print(f"{year} {event['EventName']}: {len(df)} clean laps")

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _laps_to_rows(ses, year: int, circuit: str) -> pd.DataFrame | None:
    laps = ses.laps
    if laps is None or laps.empty:
        return None

    laps = laps.copy()
    laps["LapTimeS"] = laps["LapTime"].dt.total_seconds()

    # drop pit in/out, deleted, and missing-time laps
    clean = laps[
        laps["LapTimeS"].notna()
        & laps["PitInTime"].isna() & laps["PitOutTime"].isna()
        & (laps.get("Deleted", pd.Series(False, index=laps.index)) != True)  # noqa: E712
    ].copy()
    if clean.empty:
        return None

    # remove obvious safety-car / traffic laps: > median + 5s within each stint
    def _flag(group: pd.DataFrame) -> pd.DataFrame:
        med = group["LapTimeS"].median()
        return group[group["LapTimeS"] < med + 5.0]
    clean = clean.groupby(["Driver", "Stint"], group_keys=False).apply(_flag)

    # fuel correction to lap 1 equivalent
    clean["FuelCorrectedS"] = clean["LapTimeS"] - FUEL_EFFECT_S_PER_LAP * (
        clean["LapNumber"].max() - clean["LapNumber"])

    weather = _mean_weather(ses)
    out = pd.DataFrame({
        "year": year,
        "circuit": circuit,
        "driver": clean["Driver"].values,
        "team": clean["Team"].values,
        "compound": clean["Compound"].values,
        "tyre_age": clean["TyreLife"].values,
        "stint": clean["Stint"].values,
        "lap_number": clean["LapNumber"].values,
        "lap_time_s": clean["LapTimeS"].values,
        "fuel_corrected_s": clean["FuelCorrectedS"].values,
        "track_temp": weather.get("TrackTemp", np.nan),
        "air_temp": weather.get("AirTemp", np.nan),
    })
    return out


def _mean_weather(ses) -> dict:
    try:
        w = ses.weather_data
        return {"TrackTemp": float(w["TrackTemp"].mean()),
                "AirTemp": float(w["AirTemp"].mean())}
    except Exception:  # noqa: BLE001
        return {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2023, 2024])
    ap.add_argument("--out", default="data/stints.parquet")
    args = ap.parse_args()

    df = build(args.years)
    if df.empty:
        raise SystemExit("No data built — check FastF1 availability / cache.")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows -> {args.out}")
