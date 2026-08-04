"""
Train + compare tire-degradation models with chronological walk-forward validation.

    python train.py --data data/stints.parquet --out models/tire_model.pkl
    python train.py --limit-models linear lightgbm      # quick iteration

Two targets are evaluated, because the obvious one does not work (Phase 3 finding):

* ``absolute`` — ``fuel_corrected_s``, the column `build_dataset.py` advertises as the
  target. Kept only so the failure is on the record. Two ways it fails:

  - *with* ``previous_lap_pace`` the model degenerates into persistence: it copies the
    last lap, scores a flattering MAE, and its implied degradation is ~0.006 s/lap
    against the ~0.04 s/lap actually in the data. Useless to serve.
  - *without* it the model fingerprints each race through the continuous weather
    columns (every race has a near-unique track/air-temp trace), fits the training
    races to ~0.4 s and lands ~14 s out on a race it has never seen.

* ``delta`` — **shipped.** Lap time above the stint's own reference pace (median of its
  first ``REF_LAPS`` clean laps). Removes circuit/car/fuel level, so what is left to
  learn is the degradation curve itself and it transfers across races. `tires.py`
  anchors the level on the stint's observed laps, which it already has, and the slope
  across the horizon — the number the product actually shows — is unchanged by the
  anchor.

Every candidate is scored against two trivial baselines on identical rows:
``persistence`` (last lap repeats) and ``stint_linear`` (the per-stint straight line
`backend/app/services/tires.py` ships today). An ML model that cannot beat those is not
worth loading, and the artifact records ``beats_baseline`` either way.
"""
from __future__ import annotations

import argparse
import os
from datetime import date

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

# one stint = one driver's run on one set of tyres in one race (matches build_dataset.py)
STINT_KEYS = ["year", "round", "driver", "stint"]
# laps of a stint used to establish its reference pace. Also the warm-up the per-stint
# baseline needs before it can extrapolate, so scoring starts after them for everyone.
REF_LAPS = 5
# a fold needs this many races behind it before its verdict means anything
MIN_TRAIN_RACES = 5
# Rolling training window, in races. Expanding-window (train on *everything* earlier)
# is the textbook default, but F1 cars, tyre compounds and regulations move between and
# within seasons, so 2023 round 1 is weak evidence about 2024 round 20 — and the window
# also keeps a 45-fold sweep across five candidates down to a couple of minutes.
# Set --max-train-races 0 for a true expanding window.
MAX_TRAIN_RACES = 12
# minimum training rows for a fold to be scored at all
MIN_TRAIN_ROWS = 100
# laps of a stint needed before its own degradation slope is worth fitting
MIN_STINT_LAPS = 8
# horizon used for the implied-degradation check, in laps
SLOPE_HORIZON = 10
# rows fed to SHAP. Exact tree SHAP is superlinear in trees x samples and we only want
# the ranking, which is stable well below this.
SHAP_SAMPLE = 200

CAT = ["compound", "circuit", "driver", "team"]
# The doc's full feature set, used for the absolute-target comparison only.
NUM_ABSOLUTE = ["tyre_age", "lap_number", "stint", "track_temp", "air_temp",
                "previous_lap_pace", "laps_since_safety_car", "gap_ahead_s"]
# Shipped model. `previous_lap_pace` is dropped on purpose: it collapses the model into
# persistence and, worse, cannot be fed forward across a horizon at serving time without
# recursion, which flattens the predicted degradation curve to nothing. `lap_number` is
# dropped because `tyre_age` already carries the within-stint axis, and lap_number mostly
# invites memorising one race's pace profile.
NUM_DELTA = ["tyre_age", "stint", "track_temp", "air_temp",
             "laps_since_safety_car", "gap_ahead_s"]

TARGETS = {"absolute": NUM_ABSOLUTE, "delta": NUM_DELTA}


# --------------------------------------------------------------------------- data


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the race ordering, the per-stint reference pace and the delta target."""
    df = df.sort_values(["year", "round", "driver", "stint", "tyre_age"])
    df = df.reset_index(drop=True)
    # Chronological. The old sort was ["year", "circuit"], which alphabetises circuits
    # within a season — "train on earlier races" was actually "train on circuits earlier
    # in the alphabet", so the no-leakage claim was never actually being tested.
    df["race"] = df.groupby(["year", "round"], sort=False).ngroup()

    g = df.groupby(STINT_KEYS, sort=False)["fuel_corrected_s"]
    df["stint_ref_s"] = g.transform(lambda s: s.head(REF_LAPS).median())
    df["delta"] = df["fuel_corrected_s"] - df["stint_ref_s"]
    # rank within the stint, so both the baselines and the scoring mask can skip the
    # warm-up laps the per-stint baseline needs in order to have a line at all
    df["lap_in_stint"] = df.groupby(STINT_KEYS, sort=False).cumcount()
    return df


def target_column(target: str) -> str:
    return "fuel_corrected_s" if target == "absolute" else "delta"


# ---------------------------------------------------------------------- baselines


def baseline_persistence(train: pd.DataFrame, test: pd.DataFrame,
                         target: str) -> np.ndarray:
    """This lap will be the same as the last one. Deceptively hard to beat."""
    pred = test["previous_lap_pace"].to_numpy(dtype=float)
    if target == "delta":
        pred = pred - test["stint_ref_s"].to_numpy(dtype=float)
    return pred


def baseline_stint_linear(train: pd.DataFrame, test: pd.DataFrame,
                          target: str) -> np.ndarray:
    """The straight line `backend/app/services/tires.py` ships.

    Expanding window, because that is what the live path really does: to predict lap
    *n* of a stint it refits on every clean lap of that stint up to *n-1* — all of them,
    not a fixed warm-up — then extrapolates one lap. Scoring a single fit from the first
    `REF_LAPS` laps across the whole stint would flatter the ML models against a
    baseline nobody ships.
    """
    col = target_column(target)
    pred = np.full(len(test), np.nan)
    pos = {k: i for i, k in enumerate(test.index)}
    for _, g in test.groupby(STINT_KEYS, sort=False):
        ages = g["tyre_age"].to_numpy(dtype=float)
        vals = g[col].to_numpy(dtype=float)
        for j, idx in enumerate([pos[i] for i in g.index]):
            if j < 2:                        # not enough history for a line yet
                pred[idx] = vals[0]
                continue
            slope, intercept = np.polyfit(ages[:j], vals[:j], 1)
            pred[idx] = slope * ages[j] + intercept
    return pred


BASELINES = {"persistence": baseline_persistence, "stint_linear": baseline_stint_linear}


# ------------------------------------------------------------------------- models


def candidates(only: list[str] | None = None) -> dict:
    m = {
        "linear": LinearRegression(),
        # leaf size and max_features are regularisers, not just speed: lap times are
        # noisy and an unbounded 300-tree forest mostly memorises individual laps
        "random_forest": RandomForestRegressor(n_estimators=150, min_samples_leaf=20,
                                               max_features=0.5, max_samples=0.7,
                                               n_jobs=-1, random_state=0),
        "gradient_boosting": GradientBoostingRegressor(random_state=0),
    }
    try:
        from xgboost import XGBRegressor
        m["xgboost"] = XGBRegressor(n_estimators=400, learning_rate=0.05, max_depth=5,
                                    n_jobs=-1, random_state=0)
    except ImportError:
        print("xgboost not installed — skipping")
    try:
        from lightgbm import LGBMRegressor
        m["lightgbm"] = LGBMRegressor(n_estimators=500, learning_rate=0.05,
                                      random_state=0, verbose=-1)
    except ImportError:
        print("lightgbm not installed — skipping")
    return {k: v for k, v in m.items() if only is None or k in only}


def make_pipeline(model, num: list[str]) -> Pipeline:
    pre = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT),
        ("num", "passthrough", num),
    ])
    return Pipeline([("pre", pre), ("model", model)])


# ------------------------------------------------------------------ walk-forward


def train_slice(df: pd.DataFrame, race: int, max_train: int) -> pd.DataFrame:
    """The races a fold testing on `race` is allowed to learn from."""
    lo = 0 if max_train <= 0 else race - max_train
    return df[(df["race"] < race) & (df["race"] >= lo)]


def walk_forward(df: pd.DataFrame, model, target: str,
                 max_train: int = MAX_TRAIN_RACES) -> dict:
    """Train on every race before race *i*, test on race *i*. Genuinely chronological.

    `model` is either an sklearn estimator or a baseline callable. Both are scored on
    the same rows — laps past the stint warm-up — so the comparison is like for like.
    Returns per-fold metrics plus the pooled out-of-fold residuals, which the artifact
    carries so `tires.py` can build its confidence band from held-out error instead of
    from the spread of the single stint in front of it.
    """
    num, col = TARGETS[target], target_column(target)
    races = np.sort(df["race"].unique())
    folds, residuals = [], []

    for i in range(MIN_TRAIN_RACES, len(races)):
        # boolean masks over a precomputed race index — the old code rebuilt a
        # MultiIndex and ran an O(n) `.isin` membership scan on every fold
        train = train_slice(df, races[i], max_train)
        test = df[(df["race"] == races[i]) & (df["lap_in_stint"] >= REF_LAPS)]
        if len(train) < MIN_TRAIN_ROWS or test.empty:
            continue

        if callable(model):
            full = df[df["race"] == races[i]]          # baselines need the warm-up laps
            pred = pd.Series(model(train, full, target),
                             index=full.index)[test.index].to_numpy(dtype=float)
        else:
            pipe = make_pipeline(model, num)
            pipe.fit(train[num + CAT], train[col])
            pred = pipe.predict(test[num + CAT])

        residuals.append(test[col].to_numpy(dtype=float) - pred)
        folds.append({
            "race": int(races[i]),
            "year": int(test["year"].iloc[0]),
            "round": int(test["round"].iloc[0]),
            "circuit": str(test["circuit"].iloc[0]),
            "n": int(len(test)),
            "mae": float(mean_absolute_error(test[col], pred)),
            "rmse": float(np.sqrt(mean_squared_error(test[col], pred))),
        })

    if not folds:
        return {"folds": [], "mae": float("nan"), "rmse": float("nan"),
                "mae_std": float("nan"), "residuals": np.array([])}
    maes = np.array([f["mae"] for f in folds])
    return {
        "folds": folds,
        "mae": float(maes.mean()),
        "rmse": float(np.mean([f["rmse"] for f in folds])),
        "mae_std": float(maes.std()),
        "residuals": np.concatenate(residuals),
    }


def implied_degradation(pipe: Pipeline, sample: pd.DataFrame, num: list[str]) -> float:
    """s/lap the model *thinks* the tyre is losing: re-predict the same laps with
    `tyre_age` pushed out by SLOPE_HORIZON, holding everything else fixed.

    This matters more than MAE. A model can score beautifully by copying the previous
    lap and still be flat in tyre age — which would make `degradation_s_per_lap`, the
    number the dashboard puts on screen, come out ~zero on every stint.
    """
    older = sample.copy()
    older["tyre_age"] = older["tyre_age"] + SLOPE_HORIZON
    delta = pipe.predict(older[num + CAT]) - pipe.predict(sample[num + CAT])
    return float(np.mean(delta) / SLOPE_HORIZON)


def measured_degradation(df: pd.DataFrame, target: str) -> float:
    """The slope actually present in the data, fitted within each stint and pooled.
    A single fit across stints would measure circuit speed, not degradation."""
    col = target_column(target)
    slopes = [float(np.polyfit(g["tyre_age"], g[col], 1)[0])
              for _, g in df.groupby(STINT_KEYS, sort=False) if len(g) >= MIN_STINT_LAPS]
    return float(np.median(slopes)) if slopes else float("nan")


# ------------------------------------------------------------------ explainability


def shap_importances(pipe: Pipeline, sample: pd.DataFrame, num: list[str]) -> dict:
    """Mean |SHAP| per *original* column (one-hot levels summed back together)."""
    try:
        import shap
    except ImportError:
        print("shap not installed — skipping feature importances")
        return {}
    pre, model = pipe.named_steps["pre"], pipe.named_steps["model"]
    x = pre.transform(sample[num + CAT])
    x = x.toarray() if hasattr(x, "toarray") else x
    try:
        values = shap.Explainer(model)(x).values
    except Exception as e:  # noqa: BLE001
        print(f"shap unavailable for this model ({e})")
        return {}
    out: dict[str, float] = {}
    for name, v in zip(pre.get_feature_names_out(), np.abs(values).mean(axis=0)):
        base = name.split("__", 1)[1]
        # one-hot columns come back as "compound_SOFT" — fold them into "compound"
        col = next((c for c in CAT if base.startswith(c + "_")), base)
        out[col] = out.get(col, 0.0) + float(v)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def check_leakage(df: pd.DataFrame) -> None:
    """`previous_lap_pace` must come from a strictly earlier lap of the same stint.

    Note `build_dataset.py` already drops each stint's true first lap (it has no
    previous pace), so row 0 of a stint here is its *second* clean lap and legitimately
    carries a value — that is not the leak to look for. The leak would be the row's own
    lap time showing up in the feature.
    """
    prior = df.groupby(STINT_KEYS, sort=False)["fuel_corrected_s"].shift(1)
    has_prior = prior.notna()
    mismatch = int((prior[has_prior] != df.loc[has_prior, "previous_lap_pace"]).sum())
    self_leak = int((df["previous_lap_pace"] == df["fuel_corrected_s"]).sum())
    print(f"leakage check: previous_lap_pace != prior lap on {mismatch} rows "
          f"(must be 0), equals its own lap on {self_leak}/{len(df)} rows "
          f"(a handful are genuine ties at 1 ms resolution; a large share is a leak)")


# ------------------------------------------------------------------------ reporting


def print_table(target: str, results: dict, measured: float) -> None:
    print(f"\n=== target: {target} ({target_column(target)}) ===")
    print(f"{'model':20s} {'MAE':>8s} {'RMSE':>8s} {'MAE sd':>8s} {'folds':>6s} "
          f"{'implied s/lap':>14s}")
    for name, r in results.items():
        slope = r.get("implied_slope")
        s = "      baseline" if slope is None else f"{slope:+14.4f}"
        print(f"{name:20s} {r['mae']:8.3f} {r['rmse']:8.3f} {r['mae_std']:8.3f} "
              f"{len(r['folds']):6d} {s}")
    print(f"{'(measured in data)':20s} {'':8s} {'':8s} {'':8s} {'':6s} {measured:+14.4f}")


def write_model_card(artifact: dict, out: str, all_results: dict) -> None:
    """Same idea as `build_dataset.py`'s DATASET_CARD: the numbers that justify the
    artifact live next to it, not in someone's terminal scrollback."""
    path = os.path.join(os.path.dirname(out) or ".", "MODEL_CARD.md")

    def table(results: dict) -> str:
        rows = []
        for name, r in results.items():
            slope = r.get("implied_slope")
            rows.append(f"| {name} | {r['mae']:.3f} | {r['rmse']:.3f} | "
                        f"{r['mae_std']:.3f} | "
                        f"{'baseline' if slope is None else f'{slope:+.4f}'} |")
        return "\n".join(rows)

    m, d = artifact["metrics"], artifact["dataset"]
    worst = sorted(m["folds"], key=lambda f: -f["mae"])[:5]
    verdict = ("**beats both trivial baselines**, so it is worth loading."
               if artifact["beats_baseline"] else
               "**does not beat either trivial baseline**, so `tires.py` should keep "
               "serving `baseline-linear` until this changes.")
    with open(path, "w") as f:
        f.write(f"""# Model card — `{os.path.basename(out)}`

Written by `ml/train.py` on {artifact['trained_on']}. Do not edit by hand.

| | |
|---|---|
| Model | `{artifact['model_name']}` |
| Target | `{artifact['target']}` |
| Trained on | {d['rows']} laps · {d['races']} races · {', '.join(map(str, d['years']))} |
| Walk-forward MAE | {m['mae']:.3f} s (sd across races {m['mae_std']:.3f}) |
| Walk-forward RMSE | {m['rmse']:.3f} s |
| Beats baseline | {artifact['beats_baseline']} |

{artifact['target_doc']}

## Validation

Chronological walk-forward, one fold per race, ordered by `(year, round)` — **not** by
circuit name, which alphabetises rather than sorts by time. Each fold trains on the
{MAX_TRAIN_RACES} races immediately before its test race and is scored only on laps past
the first {REF_LAPS} of a stint, so the per-stint baseline has a fit to extrapolate from
and every candidate is judged on identical rows. {len(m['folds'])} folds.

### Shipped target (`delta` — seconds above the stint's own reference pace)

| model | MAE (s) | RMSE (s) | MAE sd | implied s/lap |
|---|---|---|---|---|
{table(all_results['delta'])}
| *(slope measured in the data)* | | | | {artifact['measured_degradation_s_per_lap']:+.4f} |

### Rejected target (`absolute` — `fuel_corrected_s`), kept for the record

| model | MAE (s) | RMSE (s) | MAE sd | implied s/lap |
|---|---|---|---|---|
{table(all_results['absolute'])}

Predicting absolute lap time does not work. Without `previous_lap_pace` the models
fingerprint each race through the continuous weather columns and land seconds out on a
race they have never seen; with it they degenerate into copying the previous lap, which
scores well and predicts no degradation at all. Both failures are visible above: every
absolute-target MAE is far worse than the trivial baselines, and the implied degradation
collapses toward zero.

## Verdict

The best model is `{artifact['model_name']}` at {m['mae']:.3f} s, and it {verdict}

| baseline | MAE (s) |
|---|---|
""" + "\n".join(f"| {k} | {v:.3f} |" for k, v in artifact["baseline_mae"].items()) + f"""

`persistence` = "this lap will match the last one". `stint_linear` = the expanding-window
straight line `backend/app/services/tires.py` already ships. Neither knows anything about
tyres; both win. A cross-race model has to infer a stint's condition from circuit, car and
weather, while the baselines simply read that stint's own recent laps — on a 1-lap horizon
that is a large and probably decisive advantage.

## Implied degradation

Re-predicting the same laps with `tyre_age` pushed out {SLOPE_HORIZON} laps and everything
else held fixed gives {artifact['implied_degradation_s_per_lap']:+.4f} s/lap, against
{artifact['measured_degradation_s_per_lap']:+.4f} s/lap actually present in the data.
Right sign, right order of magnitude, roughly a third short. This matters more than MAE:
it is the number `degradation_s_per_lap` is derived from, and a model can score a
flattering MAE while being completely flat in tyre age.

## Feature importances (mean |SHAP|, one-hot levels summed per column)

| feature | mean \\|SHAP\\| |
|---|---|
""" + "\n".join(f"| {k} | {v:.4f} |"
                for k, v in artifact["feature_importances"].items()) + f"""

`tyre_age` is essentially tied with `circuit` for first, and `driver`/`team` sit near the
bottom — so this is not purely a circuit-lookup table. But `compound` ranking last is
wrong: `DATASET_CARD.md` measures SOFT degrading about twice as fast as HARD, and the
model is not picking that up.

## Worst folds

| race | n | MAE (s) |
|---|---|---|
""" + "\n".join(f"| {w['year']} R{w['round']} {w['circuit']} | {w['n']} | {w['mae']:.3f} |"
                for w in worst) + """

## Reproduce

    pip install -r requirements.txt
    python build_dataset.py --years 2023 2024 --plot
    python train.py --cache-clear
""")
    print(f"Wrote {path}")


def print_folds(name: str, folds: list[dict]) -> None:
    print(f"\nper-fold MAE — {name} (variance across races is the point of this table)")
    for f in folds:
        print(f"  {f['year']} R{f['round']:<2d} {f['circuit'][:28]:28s} "
              f"n={f['n']:4d}  MAE={f['mae']:6.3f}  RMSE={f['rmse']:6.3f}")


# ----------------------------------------------------------------------------- main


def cached(path: str | None, key: tuple, compute):
    """Memoise one (target, model) sweep on disk so a re-run only redoes what changed.

    ponytail: a plain dict in a joblib file, no invalidation. Delete the cache file
    after editing features or model params — that is what --cache-clear is for.
    """
    if not path:
        return compute()
    store = joblib.load(path) if os.path.exists(path) else {}
    if key not in store:
        store[key] = compute()
        joblib.dump(store, path)
    return store[key]


def evaluate(df: pd.DataFrame, target: str, only: list[str] | None,
             max_train: int, cache: str | None) -> dict:
    num, col = TARGETS[target], target_column(target)
    results: dict[str, dict] = {}

    for name, fn in BASELINES.items():
        results[name] = cached(cache, (target, name, max_train),
                               lambda fn=fn: walk_forward(df, fn, target, max_train))
        print(f"  {name:20s} MAE={results[name]['mae']:.3f}")

    # held-out slice for the sensitivity sweep — the last race, never trained on
    last = int(df["race"].max())
    sample = df[df["race"] == last]

    for name, model in candidates(only).items():
        def run(model=model):
            r = walk_forward(df, model, target, max_train)
            fit = train_slice(df, last, max_train)
            pipe = make_pipeline(model, num).fit(fit[num + CAT], fit[col])
            r["implied_slope"] = implied_degradation(pipe, sample, num)
            return r                      # pipeline deliberately not cached: too big
        results[name] = r = cached(cache, (target, name, max_train), run)
        print(f"  {name:20s} MAE={r['mae']:.3f}  implied={r['implied_slope']:+.4f} s/lap")
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/stints.parquet")
    ap.add_argument("--out", default="models/tire_model.pkl")
    ap.add_argument("--limit-models", nargs="+", default=None,
                    help="only train these candidates (quick iteration)")
    ap.add_argument("--max-train-races", type=int, default=MAX_TRAIN_RACES,
                    help="rolling training window in races; 0 = expanding window")
    ap.add_argument("--cache", default="models/.walkforward_cache.pkl",
                    help="memoise per-(target, model) sweeps; '' to disable")
    ap.add_argument("--cache-clear", action="store_true",
                    help="drop the cache first (do this after changing features)")
    args = ap.parse_args()
    if args.cache_clear and args.cache and os.path.exists(args.cache):
        os.remove(args.cache)
    if args.cache:
        os.makedirs(os.path.dirname(args.cache) or ".", exist_ok=True)

    raw = pd.read_parquet(args.data)
    df = prepare(raw.dropna(subset=["fuel_corrected_s", *NUM_ABSOLUTE]))
    races = int(df["race"].nunique())
    print(f"Loaded {len(df)} rows · {races} races · {df['year'].min()}–{df['year'].max()}")
    check_leakage(df)

    all_results = {}
    for target in TARGETS:
        print(f"\nwalk-forward, target={target} …")
        all_results[target] = evaluate(df, target, args.limit_models,
                                       args.max_train_races, args.cache or None)
        print_table(target, all_results[target], measured_degradation(df, target))

    # The absolute target is evaluated for the record only; the shipped artifact is the
    # delta model, for the reasons in the module docstring.
    results = all_results["delta"]
    ml = {k: v for k, v in results.items() if k not in BASELINES and v["folds"]}
    if not ml:
        raise SystemExit("No candidate trained successfully.")
    best = min(ml, key=lambda k: ml[k]["mae"])
    baseline_mae = min(results[b]["mae"] for b in BASELINES)
    beats = bool(ml[best]["mae"] < baseline_mae)

    print_folds(best, ml[best]["folds"])
    print(f"\nBest ML: {best}  MAE={ml[best]['mae']:.3f}s  "
          f"best baseline={baseline_mae:.3f}s  beats_baseline={beats}")

    # Refit on everything for serving; walk-forward was for measurement only.
    num = TARGETS["delta"]
    final = make_pipeline(candidates([best])[best], num).fit(df[num + CAT], df["delta"])

    importances = shap_importances(
        final, df[df["race"] == df["race"].max()].head(SHAP_SAMPLE), num)
    if importances:
        print("\nmean |SHAP| per feature (of the shipped model):")
        for k, v in importances.items():
            print(f"  {k:22s} {v:.4f}")
    resid = ml[best]["residuals"]
    artifact = {
        "pipeline": final,
        "model_name": best,
        "target": "delta",
        "target_doc": ("prediction is seconds ABOVE the stint's reference pace, where "
                       "the reference is the median of the stint's first "
                       f"{REF_LAPS} clean fuel-corrected laps. tires.py adds its own "
                       "reference back to get wall-clock seconds."),
        "ref_laps": REF_LAPS,
        "numeric_features": num,
        "categorical_features": CAT,
        "features": num + CAT,
        # tires.py imputes the features it cannot cheaply fetch per request
        # (gap_ahead_s, laps_since_safety_car) from these
        "feature_medians": {c: float(df[c].median()) for c in num},
        "metrics": {"mae": ml[best]["mae"], "rmse": ml[best]["rmse"],
                    "mae_std": ml[best]["mae_std"], "folds": ml[best]["folds"]},
        "baseline_mae": {b: results[b]["mae"] for b in BASELINES},
        "beats_baseline": beats,
        "implied_degradation_s_per_lap": ml[best]["implied_slope"],
        "measured_degradation_s_per_lap": measured_degradation(df, "delta"),
        # held-out error, for tires.py's confidence band — the live path currently
        # widens by ±1.96σ of the stint in front of it, which is not a validated number
        "residual_quantiles": {str(q): float(np.quantile(resid, q))
                               for q in (0.05, 0.25, 0.5, 0.75, 0.95)},
        "feature_importances": importances,
        "dataset": {"path": args.data, "rows": int(len(df)), "races": races,
                    "years": sorted(int(y) for y in df["year"].unique())},
        "trained_on": date.today().isoformat(),
        "absolute_target_results": {
            k: {"mae": v["mae"], "rmse": v["rmse"], "implied_slope": v.get("implied_slope")}
            for k, v in all_results["absolute"].items()},
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    joblib.dump(artifact, args.out)
    write_model_card(artifact, args.out, all_results)
    print(f"\nSaved -> {args.out}"
          + ("" if beats else "  (beats_baseline=False — tires.py must check this flag "
                              "before preferring the model over the baseline)"))


if __name__ == "__main__":
    main()
