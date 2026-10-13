"""Phase 6D runs on the development races (the last 3 races by date stay untouched until `final`).

    cd backend && python -m scripts.run_6d defaults  [--jobs 8] [--allow-dirty]
    cd backend && python -m scripts.run_6d tuned     [--jobs 8]
    cd backend && python -m scripts.run_6d intervals [--jobs 8]
    cd backend && python -m scripts.run_6d final     [--jobs 8]

Every stage: refuse a dirty git tree (unless --allow-dirty), evaluate, SAVE the result to
data/tires/eval/ and print it, THEN log to MLflow experiment "raceiq-tires" (a logging failure can
never discard a long computation).
"""
from __future__ import annotations

import os

# One thread per worker process: 8 fold workers × per-library thread pools oversubscribe the CPU.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import pickle  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import warnings  # noqa: E402
from collections.abc import Callable, Sequence  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pandas as pd  # noqa: E402

from app.config import settings  # noqa: E402
from app.tires.bar import check  # noqa: E402
from app.tires.evaluation import EvalResult, evaluate, race_order, split_holdout  # noqa: E402
from app.tires.models.forecasting import ForecastModel  # noqa: E402
from app.tires.models.persistence import PersistenceModel  # noqa: E402
from app.tires.models.tree_models import FACTORIES, make_tree_forecaster  # noqa: E402
from app.tires.report import latest_outputs  # noqa: E402
from scripts.run_eval import EXPERIMENT, REPO, git  # noqa: E402

HOLDOUT_RACES = 3
RESULTS_DIR = REPO / "data" / "tires" / "eval"


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


def comparison_table(result: EvalResult) -> str:
    s = result.summary
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


def verdict_table(result: EvalResult) -> str:
    if "loro" not in set(result.summary["split"]):
        return ""
    mark = {True: "✅", False: "❌", None: "n/a"}
    lines = ["| Model | 1: MAE ≤ 0.9 × persistence | 2: abs(bias) ≤ 0.05 | 3: WF ≤ persistence | 4: wins majority | Passes |",
             "|---|---|---|---|---|---|"]
    for v in sorted(check(result.summary, result.by_race), key=lambda v: v.loro_mae):
        c = v.checks
        wf = f"{v.wf_mae:.3f} vs ≤ {v.wf_target:.3f}" if v.wf_mae is not None and v.wf_target is not None else ""
        lines.append(f"| {v.model} | {mark[c['1_mae_10pct_below_persistence']]} {v.loro_mae:.3f} vs ≤ {v.loro_target:.3f} | "
                     f"{mark[c['2_bias_within_0.05']]} {v.bias:+.3f} | {mark[c['3_walk_forward_not_worse']]} {wf} | "
                     f"{mark[c['4_wins_majority_of_races']]} {v.races_won}/{v.races_total} | {'**yes**' if v.passes else 'no'} |")
    return "\n".join(lines)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_6d")
    parser.add_argument("stage", choices=["defaults"])
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--no-mlflow", action="store_true")
    args = parser.parse_args(argv)

    dirty = git("status", "--porcelain")
    if dirty and not args.allow_dirty:
        print("refusing to run: the git tree is dirty (commit first, or pass --allow-dirty):\n" + dirty, file=sys.stderr)
        return 2
    frame, _log, meta = latest_outputs(Path(args.data_dir) / "tires", None)
    dev, holdout = split_holdout(frame, HOLDOUT_RACES)
    held = race_order(holdout)
    print(f"development races: {dev['session_key'].nunique()}; held out, untouched: {', '.join(held)}", flush=True)
    base_params: dict[str, object] = {"development_races": dev["session_key"].nunique(), "holdout": ",".join(held),
                                      "target": "lap_time - mean(last 3 laps)"}

    if args.stage == "defaults":
        models: list[ForecastModel] = [PersistenceModel()] + [make_tree_forecaster(k, identity=i)
                                                              for k in FACTORIES for i in (False, True)]
        run_stage("defaults", dev, models, splits=("loro", "walk_forward"), horizons=(5, 10), jobs=args.jobs, meta=meta,
                  dirty=dirty, use_mlflow=not args.no_mlflow, extra_params={**base_params, "hyperparameters": "library defaults"},
                  report=lambda r: comparison_table(r) + "\n\n" + verdict_table(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
