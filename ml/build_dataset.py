"""
Build the tire-degradation training set from historical races via FastF1 (Person 3).

    python build_dataset.py --years 2023 2024 --out data/stints.parquet
    python build_dataset.py --years 2024 --limit-rounds 3 --plot        # quick iteration

One row per clean race lap. Removes pit in/out laps, non-green (safety-car / VSC / red)
laps and clearly invalid laps, fuel-corrects lap times, and attaches per-lap tyre age,
compound, per-lap weather, a traffic proxy and previous-lap pace.

Column contract (what train.py and tires.py both read):

* `lap_time_s`        — raw wall-clock lap time.
* `fuel_corrected_s`  — **target.** `lap_time_s` corrected to an end-of-race fuel load,
  so the remaining trend against `tyre_age` is degradation and not fuel burn-off.
  Same `FUEL_EFFECT_S_PER_LAP` as `backend/app/services/tires.py` — the two MUST agree
  or the model trains on one definition of "corrected" and serves another.
* `previous_lap_pace` — the *previous clean lap in the same stint*, fuel-corrected.
  Strictly earlier by construction, so it is safe to use at inference time.
* `laps_since_safety_car` — laps since the last non-green lap in the session; laps
  since the race start if there has not been one yet.
* `gap_ahead_s`       — traffic proxy: lap-start-time delta to the car ahead on the
  same lap. Sentinel `CLEAR_TRACK_S` when there is no car ahead.
* `round`             — needed for chronological walk-forward validation in train.py.
  Do not order races by circuit name; that alphabetises rather than sorts by time.

`_clean_and_engineer` is pure pandas and has no FastF1 dependency, so it is unit-tested
offline in `test_features.py`.
"""
from __future__ import annotations

import argparse
import os
from datetime import date

import numpy as np
import pandas as pd

CACHE_DIR = os.environ.get("RACEIQ_FASTF1_CACHE_DIR", ".fastf1_cache")

# Rough fuel-effect correction: cars get ~0.03s/lap faster as fuel burns off.
# Correcting to a common fuel load isolates tyre degradation from fuel.
FUEL_EFFECT_S_PER_LAP = 0.03
# A lap slower than its stint's median by this much is traffic/incident, not degradation.
STINT_MEDIAN_TOLERANCE_S = 5.0
# `gap_ahead_s` when the car is leading the lap — i.e. nothing in front of it.
CLEAR_TRACK_S = 99.0
# FastF1 TrackStatus digits that mean the lap was not run under green-flag conditions.
NON_GREEN_STATUS = "4567"
# Wet running is a different physical regime: on a drying track lap times *fall* with
# tyre age as the line rubbers in, so INTERMEDIATE/WET stints show a strongly negative
# slope and would teach the model that old tyres are faster. Measured on 2023-24:
# INTERMEDIATE -0.27 s/lap, WET -0.30 s/lap, versus +0.03..+0.06 for the dry compounds.
# ponytail: dropped rather than modelled — revisit only if a wet-race mode is asked for.
DRY_COMPOUNDS = ("SOFT", "MEDIUM", "HARD")

FEATURES = ["tyre_age", "lap_number", "stint", "track_temp", "air_temp",
            "previous_lap_pace", "laps_since_safety_car", "gap_ahead_s"]
TARGET = "fuel_corrected_s"
# one stint = one driver's run on one set of tyres in one race
STINT_KEYS = ["year", "round", "driver", "stint"]
# laps needed before a stint's own degradation slope is worth fitting
MIN_STINT_LAPS = 8


def build(years: list[int], session: str = "R",
          limit_rounds: int | None = None) -> pd.DataFrame:
    import fastf1  # imported here so the pure feature code stays offline-testable

    os.makedirs(CACHE_DIR, exist_ok=True)
    fastf1.Cache.enable_cache(CACHE_DIR)
    rows: list[pd.DataFrame] = []

    for year in years:
        schedule = fastf1.get_event_schedule(year)
        built = 0
        for _, event in schedule.iterrows():
            rnd = int(event["RoundNumber"])
            if rnd == 0:
                continue  # pre-season testing
            if limit_rounds is not None and built >= limit_rounds:
                break
            try:
                ses = fastf1.get_session(year, rnd, session)
                ses.load(telemetry=False, weather=True, laps=True)
            except Exception as e:  # noqa: BLE001
                print(f"skip {year} R{rnd}: {e}")
                continue
            df = _session_rows(ses, year, rnd, str(event["EventName"]))
            if df is not None and len(df):
                rows.append(df)
                built += 1
                print(f"{year} R{rnd} {event['EventName']}: {len(df)} clean laps")

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def _session_rows(ses, year: int, rnd: int, circuit: str) -> pd.DataFrame | None:
    """Flatten one FastF1 session into the columns `_clean_and_engineer` expects."""
    laps = ses.laps
    if laps is None or laps.empty:
        return None

    flat = pd.DataFrame({
        "year": year,
        "round": rnd,
        "circuit": circuit,
        "driver": laps["Driver"].to_numpy(),
        "team": laps["Team"].to_numpy(),
        "compound": laps["Compound"].to_numpy(),
        "tyre_age": pd.to_numeric(laps["TyreLife"], errors="coerce").to_numpy(),
        "stint": pd.to_numeric(laps["Stint"], errors="coerce").to_numpy(),
        "lap_number": pd.to_numeric(laps["LapNumber"], errors="coerce").to_numpy(),
        "lap_time_s": laps["LapTime"].dt.total_seconds().to_numpy(),
        "lap_start_s": laps["LapStartTime"].dt.total_seconds().to_numpy(),
        "track_status": laps["TrackStatus"].astype(str).to_numpy(),
        "is_pit": (laps["PitInTime"].notna() | laps["PitOutTime"].notna()).to_numpy(),
        "is_invalid": _invalid_mask(laps).to_numpy(),
    })
    flat = flat.join(_per_lap_weather(ses, laps))
    return _clean_and_engineer(flat)


def _invalid_mask(laps: pd.DataFrame) -> pd.Series:
    """Deleted laps, plus FastF1's own `IsAccurate` verdict where it is available."""
    deleted = laps.get("Deleted")
    bad = (pd.Series(False, index=laps.index) if deleted is None
           else deleted.fillna(False).astype(bool))
    accurate = laps.get("IsAccurate")
    if accurate is not None:
        bad = bad | ~accurate.fillna(False).astype(bool)
    return bad


def _per_lap_weather(ses, laps: pd.DataFrame) -> pd.DataFrame:
    """Per-lap track/air temp. Track temp moves several degrees across a race, so the
    old session-mean flattened one of the model's headline features into a constant."""
    try:
        w = laps.get_weather_data()  # FastF1 aligns this to the laps index for us
        return pd.DataFrame({
            "track_temp": pd.to_numeric(w["TrackTemp"], errors="coerce").to_numpy(),
            "air_temp": pd.to_numeric(w["AirTemp"], errors="coerce").to_numpy(),
        })
    except Exception as e:  # noqa: BLE001
        print(f"  per-lap weather unavailable ({e}) — falling back to session mean")
        try:
            wd = ses.weather_data
            t, a = float(wd["TrackTemp"].mean()), float(wd["AirTemp"].mean())
        except Exception:  # noqa: BLE001
            t = a = np.nan
        return pd.DataFrame({"track_temp": t, "air_temp": a}, index=range(len(laps)))


def _clean_and_engineer(flat: pd.DataFrame) -> pd.DataFrame:
    """Filter to clean green-flag laps and add the derived features. Pure pandas."""
    df = flat.copy()

    # Features that need the whole field / the unfiltered lap sequence, so they are
    # computed before anything is dropped.
    df["laps_since_safety_car"] = _laps_since_safety_car(df)
    df["gap_ahead_s"] = _gap_ahead(df)

    status = df["track_status"].fillna("1")
    green = ~status.str.contains(f"[{NON_GREEN_STATUS}]", regex=True)
    dry = df["compound"].isin(DRY_COMPOUNDS)
    df = df[df["lap_time_s"].notna() & green & dry & ~df["is_pit"] & ~df["is_invalid"]]
    if df.empty:
        return df

    # catch-all for traffic/incident laps the flags missed, per stint
    stint_median = df.groupby(["driver", "stint"])["lap_time_s"].transform("median")
    df = df[df["lap_time_s"] < stint_median + STINT_MEDIAN_TOLERANCE_S]
    if df.empty:
        return df

    # correct every lap to an end-of-race (light) fuel load
    df = df.sort_values(["driver", "stint", "lap_number"])
    df["fuel_corrected_s"] = df["lap_time_s"] - FUEL_EFFECT_S_PER_LAP * (
        df["lap_number"].max() - df["lap_number"])

    # previous *clean* lap of the same stint — strictly earlier, so no leakage
    df["previous_lap_pace"] = df.groupby(["driver", "stint"])["fuel_corrected_s"].shift(1)

    keep = ["year", "round", "circuit", "driver", "team", "compound", "tyre_age",
            "stint", "lap_number", "lap_time_s", TARGET, *FEATURES]
    out = df[list(dict.fromkeys(keep))].dropna(subset=[TARGET, *FEATURES])
    return out.reset_index(drop=True)


def _laps_since_safety_car(df: pd.DataFrame) -> pd.Series:
    """Session-wide: laps since the last non-green lap (since the start if none yet)."""
    status = df["track_status"].fillna("1")
    sc_laps = df.loc[
        status.str.contains(f"[{NON_GREEN_STATUS}]", regex=True), "lap_number"].unique()
    laps = np.sort(df["lap_number"].dropna().unique())
    last_sc = pd.Series(np.where(np.isin(laps, sc_laps), laps, np.nan),
                        index=laps).ffill().fillna(0.0)
    return df["lap_number"].map(pd.Series(laps - last_sc.to_numpy(), index=laps))


def _gap_ahead(df: pd.DataFrame) -> pd.Series:
    """Traffic proxy: lap-start-time delta to the car ahead on the same lap.

    ponytail: a lap-start delta approximates the on-track gap. Swap in OpenF1
    `intervals` only if the trained model shows traffic actually carries weight.
    """
    order = df.sort_values(["lap_number", "lap_start_s"])
    gap = order.groupby("lap_number")["lap_start_s"].diff()
    return gap.reindex(df.index).fillna(CLEAR_TRACK_S).clip(0.0, CLEAR_TRACK_S)


def stint_slopes(df: pd.DataFrame) -> pd.DataFrame:
    """Degradation slope fitted **within each stint**, one row per stint.

    Fitting one line per compound across the whole file does not measure degradation:
    a Bahrain lap is ~11s slower than a Melbourne lap, so a pooled fit is dominated by
    which circuits happened to run which compound. Only a within-stint fit holds
    circuit, driver and car constant.
    """
    rows = []
    for _, g in df.groupby(STINT_KEYS + ["compound"], sort=False):
        if len(g) < MIN_STINT_LAPS:
            continue
        rows.append({"compound": g["compound"].iloc[0], "laps": len(g),
                     "slope": float(np.polyfit(g["tyre_age"], g[TARGET], 1)[0])})
    return pd.DataFrame(rows, columns=["compound", "laps", "slope"])


def _slope_summary(df: pd.DataFrame) -> pd.DataFrame:
    s = stint_slopes(df)
    if s.empty:
        return s
    return s.groupby("compound")["slope"].agg(
        stints="size", median="median", frac_positive=lambda x: (x > 0).mean())


def report(df: pd.DataFrame) -> None:
    """The sanity check that matters: does lap time actually rise with tyre age?"""
    print(f"\n{len(df)} rows · {df.groupby(['year', 'round']).ngroups} races · "
          f"{df['year'].min()}–{df['year'].max()}")
    print("\nrows per race:")
    print(df.groupby(["year", "round", "circuit"]).size().to_string())
    print("\ncompound distribution:")
    print(df["compound"].value_counts().to_string())
    print("\ntyre age:")
    print(df["tyre_age"].describe().to_string())
    print("\nwithin-stint degradation slope (s/lap vs tyre age) — median must be positive:")
    summary = _slope_summary(df)
    if summary.empty:
        print(f"  no stint had {MIN_STINT_LAPS}+ clean laps — nothing to check")
        return
    for compound, r in summary.iterrows():
        flag = "" if r["median"] > 0 else "   <-- NEGATIVE: check fuel correction / filtering"
        print(f"  {str(compound):<8} {r['median']:+.4f}  "
              f"({int(r['stints'])} stints, {r['frac_positive']:.0%} positive){flag}")


def plot(df: pd.DataFrame, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Plot each lap against its *own stint's* best, not against absolute lap time —
    # otherwise the plot just separates fast circuits from slow ones.
    d = df.copy()
    d["delta"] = d[TARGET] - d.groupby(STINT_KEYS)[TARGET].transform("min")

    fig, ax = plt.subplots(figsize=(8, 5))
    for compound, g in d.groupby("compound"):
        ax.scatter(g["tyre_age"], g["delta"], s=3, alpha=0.2, label=str(compound))
        if len(g) >= 50:
            k, b = np.polyfit(g["tyre_age"], g["delta"], 1)
            xs = np.array([g["tyre_age"].min(), g["tyre_age"].max()])
            ax.plot(xs, k * xs + b, lw=2)
    ax.set(xlabel="tyre age (laps)", ylabel="lap time vs stint best (s)",
           title="Degradation: lap time above stint best vs tyre age")
    ax.set_ylim(0, d["delta"].quantile(0.99))
    ax.legend(markerscale=5)
    fig.tight_layout()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fig.savefig(path, dpi=120)
    print(f"Wrote {path}")


def write_dataset_card(df: pd.DataFrame, out: str, args: argparse.Namespace) -> None:
    path = os.path.join(os.path.dirname(out) or ".", "DATASET_CARD.md")
    limit = f" --limit-rounds {args.limit_rounds}" if args.limit_rounds else ""
    slopes = "\n".join(
        f"| {compound} | {int(r['stints'])} | {r['median']:+.4f} | {r['frac_positive']:.0%} |"
        for compound, r in _slope_summary(df).iterrows())
    with open(path, "w") as f:
        f.write(f"""# Dataset card — `{os.path.basename(out)}`

Generated by `ml/build_dataset.py` on {date.today().isoformat()}. Do not edit by hand —
re-run the build command to regenerate.

## Provenance

| | |
|---|---|
| Command | `python build_dataset.py --years {' '.join(map(str, args.years))} --session {args.session}{limit}` |
| Source | FastF1 (`{args.session}` sessions) |
| Seasons | {', '.join(map(str, args.years))} |
| Races | {df.groupby(['year', 'round']).ngroups} |
| Rows (clean laps) | {len(df)} |
| Drivers | {df['driver'].nunique()} |
| Target | `{TARGET}` |
| Features | {', '.join(FEATURES)} |

## Filters applied

- Dropped laps with no lap time, pit in/out laps, deleted laps, and laps FastF1 marks
  `IsAccurate == False`.
- Dropped laps whose `TrackStatus` contains any of `{NON_GREEN_STATUS}` (yellow, safety
  car, VSC, red flag) — only green-flag running survives.
- Dropped INTERMEDIATE and WET laps. On a drying track lap times *fall* with tyre age
  as the line rubbers in, so wet stints carry a strongly negative slope (measured
  -0.27 and -0.30 s/lap on 2023-24) that would train the model backwards. Only
  {', '.join(DRY_COMPOUNDS)} remain.
- Dropped laps slower than their stint median by more than
  {STINT_MEDIAN_TOLERANCE_S:.1f}s (traffic / incidents the flags missed).
- Dropped the first clean lap of each stint, which has no `previous_lap_pace`.

## Fuel correction

`fuel_corrected_s = lap_time_s - {FUEL_EFFECT_S_PER_LAP} * (race_laps - lap_number)`.
The same constant lives in `backend/app/services/tires.py`;
`backend/tests/test_phase1_tires.py` asserts the two stay in sync.

## Sanity check — within-stint degradation slope

Fitted per stint (min {MIN_STINT_LAPS} clean laps), then summarised by compound. A
slope pooled across the whole file measures circuit speed, not degradation — a Bahrain
lap is ~11s slower than a Melbourne lap.

| compound | stints | median s/lap | % positive |
|---|---|---|---|
{slopes}

Every median must be positive, and softer compounds should degrade faster than harder
ones. A negative median means the fuel correction or the lap filtering is wrong, and no
model trained on this file will be trustworthy.
""")
    print(f"Wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", nargs="+", type=int, default=[2023, 2024])
    ap.add_argument("--session", default="R", help="FastF1 session code (R, Q, FP1…)")
    ap.add_argument("--limit-rounds", type=int, default=None,
                    help="only build this many rounds per season (quick iteration)")
    ap.add_argument("--out", default="data/stints.parquet")
    ap.add_argument("--plot", nargs="?", const="data/lap_time_vs_tyre_age.png",
                    default=None, help="save the lap-time-vs-tyre-age scatter")
    args = ap.parse_args()

    df = build(args.years, args.session, args.limit_rounds)
    if df.empty:
        raise SystemExit("No data built — check FastF1 availability / cache.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(f"Wrote {len(df)} rows -> {args.out}")

    report(df)
    write_dataset_card(df, args.out, args)
    if args.plot:
        plot(df, args.plot)


if __name__ == "__main__":
    main()
