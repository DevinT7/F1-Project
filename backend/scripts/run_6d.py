"""Phase 6D runs. The last 3 races by date stay untouched until `final`.

    cd backend && python -m scripts.run_6d defaults  [--jobs 8]
    cd backend && python -m scripts.run_6d tuned     [--identity] [--trials 20]
    cd backend && python -m scripts.run_6d intervals --base lightgbm [--tuned]
    cd backend && python -m scripts.run_6d explain   --base lightgbm [--tuned]
    cd backend && python -m scripts.run_6d final     --ship pooled_fixed_effects [--tuned]

Model specs: persistence | pooled_fixed_effects | <tree kind>[+identity]. --tuned uses the
hyperparameters of the full-development search saved by the `tuned` stage.

Every evaluating stage: refuse a dirty git tree (unless --allow-dirty), evaluate, SAVE the result
to data/tires/eval/ and print it, THEN log to MLflow experiment "raceiq-tires" (a logging failure
can never discard a long computation).
"""
from __future__ import annotations

import os

# One thread per worker process: 8 fold workers × per-library thread pools oversubscribe the CPU.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import pickle  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import warnings  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.config import settings  # noqa: E402
from app.tires.bar import Verdict, check, check_holdout, coverage_range  # noqa: E402
from app.tires.evaluation import HOLDOUT_RACES, EvalResult, evaluate, race_order, split_holdout  # noqa: E402
from app.tires.models.forecasting import ForecastModel, PooledFixedEffects, _pooled_fit  # noqa: E402
from app.tires.models.intervals import QuantileLightGBM, SplitConformal, conformal_quantile  # noqa: E402
from app.tires.models.persistence import PersistenceModel  # noqa: E402
from app.tires.models.tree_models import FACTORIES, TreeForecaster, make_tree_forecaster  # noqa: E402
from app.tires.report import latest_outputs  # noqa: E402
from app.tires.tuning import SEARCH_SPACES, TunedForecaster  # noqa: E402
from scripts.run_eval import EXPERIMENT, REPO, git  # noqa: E402

RESULTS_DIR = REPO / "data" / "tires" / "eval"
MODELS_DIR = REPO / "data" / "tires" / "models"
FIGURES_DIR = REPO / "docs" / "figures" / "tires"


def metric_name(*parts: object) -> str:
    """MLflow metric names allow only [A-Za-z0-9_./ -]; model names contain '+'."""
    return re.sub(r"[^A-Za-z0-9_./ -]", "_", ".".join(str(p) for p in parts))


def mlflow_module() -> Any:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    store = REPO / "mlruns"
    store.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{(store / 'mlflow.db').as_posix()}"))
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, artifact_location=(store / "artifacts").as_uri())
    mlflow.set_experiment(EXPERIMENT)
    return mlflow


# ---------------------------------------------------------------- tables
def comparison_table(result: EvalResult) -> str:
    s = result.summary
    if set(s["split"]) == {"holdout"}:
        return holdout_table(result)
    models = ["persistence"] + sorted(set(s["model"]) - {"persistence"})
    lines = ["| Model | LORO h=5 MAE | LORO h=5 bias | WF h=5 MAE | LORO h=10 MAE | WF h=10 MAE |", "|---|---|---|---|---|---|"]
    for m in models:
        def cell(split: str, h: int, col: str = "mae") -> str:
            r = s[(s["model"] == m) & (s["split"] == split) & (s["horizon"] == h)]
            if r.empty:
                return "—"
            v = float(r.iloc[0][col])
            return f"{v:+.3f}" if col == "bias" else f"{v:.3f} ± {float(r.iloc[0][col + '_se']):.3f}"
        lines.append(f"| {m} | {cell('loro', 5)} | {cell('loro', 5, 'bias')} | {cell('walk_forward', 5)} | "
                     f"{cell('loro', 10)} | {cell('walk_forward', 10)} |")
    return "\n".join(lines)


def holdout_table(result: EvalResult) -> str:
    s = result.summary
    lines = ["| Model | h=5 MAE | h=5 bias | h=10 MAE | h=5 coverage | h=5 width (s) | per-race h=5 MAE |",
             "|---|---|---|---|---|---|---|"]
    for m in ["persistence"] + sorted(set(s["model"]) - {"persistence"}):
        r5 = s[(s["model"] == m) & (s["horizon"] == 5)].iloc[0]
        r10 = s[(s["model"] == m) & (s["horizon"] == 10)]
        races = result.by_race[(result.by_race["model"] == m) & (result.by_race["horizon"] == 5)]
        per = ", ".join(f"{k} {v:.3f}" for k, v in zip(races["session_key"], races["mae"], strict=True))
        cov = "—" if pd.isna(r5["coverage"]) else f"{r5['coverage']:.1%}"
        width = "—" if pd.isna(r5["mean_width"]) else f"{r5['mean_width']:.3f}"
        h10 = f"{float(r10.iloc[0]['mae']):.3f}" if len(r10) else "—"
        lines.append(f"| {m} | {r5['mae']:.3f} | {r5['bias']:+.3f} | {h10} | {cov} | {width} | {per} |")
    return "\n".join(lines)


def interval_table(result: EvalResult) -> str:
    s = result.summary[result.summary["coverage"].notna()]
    lines = ["| Method | Split | h | Coverage | Target | Mean width (s) | Interval score | MAE |",
             "|---|---|---|---|---|---|---|---|"]
    for r in s.sort_values(["split", "horizon", "model"]).itertuples():
        lo, hi = coverage_range(str(r.model))
        lines.append(f"| {r.model} | {r.split} | {r.horizon} | {r.coverage:.1%} | {lo:.0%}–{hi:.0%} | "
                     f"{r.mean_width:.3f} | {r.interval_score:.3f} | {r.mae:.3f} |")
    return "\n".join(lines)


def choose_interval(result: EvalResult, split: str = "walk_forward", horizon: int = 5) -> str:
    """Among 80% methods whose coverage is within 72–88% when calibrated on earlier races
    (walk-forward), the narrowest."""
    s = result.summary
    s = s[(s["split"] == split) & (s["horizon"] == horizon) & s["coverage"].notna()]
    ok = s[[coverage_range(m) == (0.72, 0.88) and 0.72 <= c <= 0.88 for m, c in zip(s["model"], s["coverage"], strict=True)]]
    return str(ok.sort_values("mean_width").iloc[0]["model"]) if len(ok) else "none within tolerance"


def verdict_table(result: EvalResult, verdicts: list[Verdict] | None = None) -> str:
    if verdicts is None:
        if "loro" not in set(result.summary["split"]):
            return ""
        verdicts = check(result.summary, result.by_race)
    mark = {True: "✅", False: "❌", None: "n/a"}
    lines = ["| Model | 1: MAE ≤ target | 2: abs(bias) ≤ 0.05 | 3: forward MAE ≤ persistence | 4: wins majority | "
             "5: coverage | Passes |", "|---|---|---|---|---|---|---|"]
    for v in sorted(verdicts, key=lambda v: v.loro_mae):
        c = v.checks
        wf = f"{v.wf_mae:.3f} vs ≤ {v.wf_target:.3f}" if v.wf_mae is not None and v.wf_target is not None else ""
        cov = "" if v.coverage is None else f"{v.coverage:.1%}"
        lines.append(f"| {v.model} | {mark[c['1_mae_10pct_below_persistence']]} {v.loro_mae:.3f} vs ≤ {v.loro_target:.3f} | "
                     f"{mark[c['2_bias_within_0.05']]} {v.bias:+.3f} | {mark[c['3_walk_forward_not_worse']]} {wf} | "
                     f"{mark[c['4_wins_majority_of_races']]} {v.races_won}/{v.races_total} | "
                     f"{mark[c['5_interval_coverage_90_97']]} {cov} | {'**yes**' if v.passes else 'no'} |")
    return "\n".join(lines)


# ---------------------------------------------------------------- stages
def run_stage(stage: str, frame: pd.DataFrame, models: Sequence[ForecastModel], *, splits: Sequence[str],
              horizons: Sequence[int], jobs: int, meta: dict[str, Any], dirty: str, use_mlflow: bool,
              extra_params: dict[str, object], report: Callable[[EvalResult], str]) -> EvalResult:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = evaluate(frame, models, splits=splits, horizons=horizons, n_jobs=jobs)
    sha = git("rev-parse", "HEAD")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    saved = RESULTS_DIR / f"6d_{stage}_{meta['hash'][:12]}_{sha[:10]}.pkl"
    saved.write_bytes(pickle.dumps(result))
    text = report(result)
    print(f"[{stage}] saved {saved}\n\n{text}", flush=True)
    if use_mlflow:
        mlflow = mlflow_module()
        with mlflow.start_run(run_name=f"6d-{stage}-{meta['hash'][:12]}") as run, tempfile.TemporaryDirectory() as tmp:
            mlflow.set_tags({"dataset_hash": meta["hash"], "git_sha": sha, "git_dirty": str(bool(dirty)),
                             "fastf1_version": ",".join(meta["fastf1_versions"]), "phase": "6D", "stage": stage})
            mlflow.log_params({"splits": ",".join(splits), "horizons": ",".join(map(str, horizons)), **extra_params})
            for model in models:
                mlflow.log_params({metric_name(model.name, k): str(v)[:250] for k, v in model.params().items()})
            for r in result.summary.itertuples():
                for metric in ("mae", "mae_se", "rmse", "bias", "bias_se", "coverage", "mean_width", "interval_score",
                               "slope_mae_vs_ref"):
                    v = getattr(r, metric)
                    if pd.notna(v):
                        mlflow.log_metric(metric_name(r.model, r.split, f"h{r.horizon}", metric), float(v))
            for name in ("summary", "by_step", "by_compound", "by_race"):
                path = Path(tmp) / f"{name}.csv"
                getattr(result, name).to_csv(path, index=False)
                mlflow.log_artifact(str(path))
            (Path(tmp) / f"{stage}.md").write_text(text, encoding="utf-8")
            mlflow.log_artifact(str(Path(tmp) / f"{stage}.md"))
            print(f"\n[{stage}] MLflow run {run.info.run_id}", flush=True)
    return result


def best_params_path(meta: dict[str, Any]) -> Path:
    return RESULTS_DIR / f"6d_best_params_{meta['hash'][:12]}.json"


def load_best(meta: dict[str, Any], spec: str, tuned: bool) -> dict[str, Any]:
    if not tuned:
        return {}
    best: dict[str, dict[str, Any]] = json.loads(best_params_path(meta).read_text(encoding="utf-8"))
    return best[spec]


def point_model(spec: str, hyperparams: dict[str, Any]) -> ForecastModel:
    if spec == "persistence":
        return PersistenceModel()
    if spec == "pooled_fixed_effects":
        return PooledFixedEffects()
    kind, _, variant = spec.partition("+")
    if kind not in FACTORIES or variant not in ("", "identity"):
        raise SystemExit(f"unknown model spec {spec!r}")
    return TreeForecaster(kind, identity=variant == "identity", hyperparams=hyperparams)


def tuning_pass(dev: pd.DataFrame, specs: list[str], meta: dict[str, Any], sha: str, use_mlflow: bool,
                n_trials: int, jobs: int) -> dict[str, dict[str, Any]]:
    """Search on ALL development races (GroupKFold by race) for the hyperparameters used later.
    The honest estimate of tuned performance is the nested evaluation, not this search's CV score."""
    best: dict[str, dict[str, Any]] = {}
    trials: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        kind, _, variant = spec.partition("+")
        model = TunedForecaster(kind, identity=variant == "identity", n_trials=n_trials, n_jobs=jobs)
        model.fit(dev)
        best[spec], trials[spec] = model.best, model.trials
        print(f"[tuning] {spec}: best inner-CV MAE {min(t['inner_mae'] for t in model.trials):.4f} "
              f"(library defaults {model.trials[0]['inner_mae']:.4f}) with {model.best}", flush=True)
    best_params_path(meta).write_text(json.dumps(best, indent=2), encoding="utf-8")
    (RESULTS_DIR / f"6d_tuning_trials_{meta['hash'][:12]}.json").write_text(json.dumps(trials, indent=2), encoding="utf-8")
    if use_mlflow:
        mlflow = mlflow_module()
        with mlflow.start_run(run_name=f"6d-tuning-search-{meta['hash'][:12]}"):
            mlflow.set_tags({"dataset_hash": meta["hash"], "git_sha": sha, "phase": "6D", "stage": "tuning-search"})
            for spec, rows in trials.items():
                for t in rows:
                    with mlflow.start_run(run_name=f"{metric_name(spec)}-trial{t['trial']}", nested=True):
                        mlflow.log_params({"model": spec, **{k: v for k, v in t.items() if k != "inner_mae"}})
                        mlflow.log_metric("inner_cv_mae", t["inner_mae"])
    return best


def explain_stage(dev: pd.DataFrame, spec: str, hyperparams: dict[str, Any], meta: dict[str, Any]) -> str:
    """SHAP on the model fitted to all development races; degradation estimates (30 bootstrap
    resamples) for one stint per compound of the last development race, model fitted on the others."""
    from app.tires import degradation as D
    from app.tires.explain import explain
    from app.tires.models.base import StintContext

    model = point_model(spec, hyperparams)
    if not isinstance(model, TreeForecaster):
        raise SystemExit("explain needs a tree model spec")
    model.fit(dev)
    ex = explain(model, dev, figure_dir=FIGURES_DIR)
    lines = [f"SHAP — {model.name}, {ex.n_rows} sampled training rows", "", "| Feature | mean abs SHAP (s) |", "|---|---|"]
    lines += [f"| {f} | {v:.4f} |" for f, v in ex.ranking]
    lines += ["", f"identity share of total abs SHAP: {ex.identity_share:.1%}",
              "Spearman(feature, SHAP): " + ", ".join(f"{k} {v:+.2f}" for k, v in ex.sign_spearman.items()), ""]
    last = race_order(dev)[-1]
    train, test = dev[dev["session_key"] != last], dev[dev["session_key"] == last]
    tree = point_model(spec, hyperparams)
    assert isinstance(tree, TreeForecaster)
    tree.fit(train)
    fit_b = _pooled_fit(train)
    lines += [f"Degradation (s/lap), stints of {last}, models fitted on the other {train['session_key'].nunique()} "
              "development races, first 10 laps of each stint as history", "",
              "| Driver | Stint | Compound | Tree estimate | 95% bootstrap CI (30) | Flags | Baseline B at that age |",
              "|---|---|---|---|---|---|---|"]
    rows = []
    for compound in ("SOFT", "MEDIUM", "HARD"):
        sizes = test[test["compound"] == compound].groupby(["driver", "stint"]).size()
        sizes = sizes[sizes >= 15]
        if sizes.empty:
            continue
        d, st = sizes.sort_values(ascending=False).index[0]
        laps = test[(test["driver"] == d) & (test["stint"] == st)].sort_values("lap_number").iloc[:10]
        age = float(laps["tyre_age"].iloc[-1])
        est = D.estimate(tree, train, StintContext(last, str(d), int(st), compound, age, laps), n_boot=30)
        b = fit_b.beta_age.get(compound, float("nan")) + 2 * fit_b.gamma.get(compound, 0.0) * age
        rows.append({"driver": d, "stint": int(st), "compound": compound, "value": est.value_s_per_lap,
                     "ci": [est.ci_low, est.ci_high], "flags": list(est.flags), "baseline_b": b})
        lines.append(f"| {d} | {int(st)} | {compound} | {est.value_s_per_lap:+.3f} | "
                     f"{est.ci_low:+.3f} … {est.ci_high:+.3f} | {', '.join(est.flags) or '—'} | {b:+.3f} |")
    (RESULTS_DIR / f"6d_explain_{meta['hash'][:12]}.json").write_text(
        json.dumps({"shap": ex.to_dict(), "degradation": rows}, indent=2, default=str), encoding="utf-8")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_6d")
    parser.add_argument("stage", choices=["defaults", "tuned", "intervals", "explain", "final"])
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--no-mlflow", action="store_true")
    parser.add_argument("--identity", action="store_true", help="tuned: tune the +identity variants")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--base", default="lightgbm", help="intervals/explain: the point model spec")
    parser.add_argument("--ship", default="pooled_fixed_effects", help="final: the model spec to ship")
    parser.add_argument("--tuned", action="store_true", help="use the full-development tuned hyperparameters")
    args = parser.parse_args(argv)

    dirty = git("status", "--porcelain")
    if dirty and not args.allow_dirty:
        print("refusing to run: the git tree is dirty (commit first, or pass --allow-dirty):\n" + dirty, file=sys.stderr)
        return 2
    frame, _log, meta = latest_outputs(Path(args.data_dir) / "tires", None)
    dev, holdout = split_holdout(frame, HOLDOUT_RACES)
    held = race_order(holdout)
    print(f"development races: {dev['session_key'].nunique()}; held out: {', '.join(held)}"
          f"{' (used now: final evaluation)' if args.stage == 'final' else ', untouched'}", flush=True)
    base_params: dict[str, object] = {"development_races": dev["session_key"].nunique(), "holdout": ",".join(held),
                                      "target": "lap_time - mean(last 3 laps)"}
    both = ("loro", "walk_forward")

    def stage(name: str, data: pd.DataFrame, models: list[ForecastModel], splits: Sequence[str], extra: dict[str, object],
              report: Callable[[EvalResult], str]) -> EvalResult:
        return run_stage(name, data, models, splits=splits, horizons=(5, 10), jobs=args.jobs, meta=meta, dirty=dirty,
                         use_mlflow=not args.no_mlflow, extra_params={**base_params, **extra}, report=report)

    def standard(r: EvalResult) -> str:
        return comparison_table(r) + "\n\n" + verdict_table(r)

    if args.stage == "defaults":
        models: list[ForecastModel] = [PersistenceModel()] + [make_tree_forecaster(k, identity=i)
                                                              for k in FACTORIES for i in (False, True)]
        stage("defaults", dev, models, both, {"hyperparameters": "library defaults"}, standard)
    elif args.stage == "tuned":
        suffix = "+identity" if args.identity else ""
        models = [PersistenceModel(), *[TunedForecaster(k, identity=args.identity, n_trials=args.trials) for k in SEARCH_SPACES],
                  TreeForecaster("lightgbm", identity=args.identity, monotone_age=True)]
        stage("tuned", dev, models, both, {"tuning": f"nested: seeded random search, {args.trials} trials, "
                                                     "GroupKFold(3) by race inside each outer training set"}, standard)
        tuning_pass(dev, [k + suffix for k in SEARCH_SPACES], meta, git("rev-parse", "HEAD"), not args.no_mlflow,
                    args.trials, args.jobs)
    elif args.stage == "intervals":
        hp = load_best(meta, args.base, args.tuned)
        models = [PersistenceModel(), *[SplitConformal(m, alpha=a) for m in (PersistenceModel(), PooledFixedEffects())
                                       for a in (0.2, 0.05)],
                  *[SplitConformal(point_model(args.base, hp), alpha=a) for a in (0.2, 0.05)]]
        if args.base.startswith("lightgbm"):
            models.append(QuantileLightGBM(alpha=0.2, identity=args.base.endswith("+identity"), hyperparams=hp))
        res = stage("intervals", dev, models, both, {"base": args.base, "tuned": args.tuned},
                    lambda r: interval_table(r) + "\n\n" + verdict_table(r))
        print(f"\nchosen 80% interval method (walk-forward h=5, coverage within 72–88%, narrowest): {choose_interval(res)}")
    elif args.stage == "explain":
        print(explain_stage(dev, args.base, load_best(meta, args.base, args.tuned), meta), flush=True)
    elif args.stage == "final":
        from app.tires import artifacts

        hp = load_best(meta, args.ship, args.tuned)
        models = [PersistenceModel(), point_model(args.ship, hp),
                  *[SplitConformal(point_model(args.ship, hp), alpha=a) for a in (0.2, 0.05)]]
        if args.ship != "pooled_fixed_effects":
            models += [SplitConformal(PooledFixedEffects(), alpha=a) for a in (0.2, 0.05)]
        stage("final", frame, models, ("holdout",), {"ship": args.ship, "tuned": args.tuned},
              lambda r: comparison_table(r) + "\n\n" + verdict_table(r, check_holdout(r.summary, r.by_race)))
        shipped = SplitConformal(point_model(args.ship, hp), alpha=0.2)
        shipped.fit(frame)  # the shipped model sees all 24 races; the holdout numbers above are its estimate
        q95 = {str(s): conformal_quantile(np.asarray(r), 0.05) for s, r in shipped.residuals.items()}
        pkl, js = artifacts.save(shipped, MODELS_DIR, dataset_hash=meta["hash"], git_sha=git("rev-parse", "HEAD"),
                                 extra={"conformal_step_quantiles_95": q95, "trained_on_races": race_order(frame),
                                        "holdout_evaluated": held})
        (REPO / "docs" / "tire_model_metadata.json").write_text(js.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\nshipped {shipped.name}: {pkl}\nmetadata copied to docs/tire_model_metadata.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
