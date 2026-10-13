"""Phase 6D runs. Stage `defaults`: default-hyperparameter tree models vs the agreed bar.

    cd backend && python -m scripts.run_6d defaults [--jobs 8] [--allow-dirty]

Development races only: the last 3 races by date are held out and never touched here.
Logs to MLflow experiment "raceiq-tires" (same SQLite store as scripts.run_eval) and prints the
comparison table and the per-model bar verdicts.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import warnings
from pathlib import Path

import pandas as pd

from app.config import settings
from app.tires.bar import check
from app.tires.evaluation import EvalResult, evaluate, race_order, split_holdout
from app.tires.models.persistence import PersistenceModel
from app.tires.models.tree_models import FACTORIES, make_tree_forecaster
from app.tires.report import latest_outputs
from scripts.run_eval import EXPERIMENT, REPO, git

HOLDOUT_RACES = 3


def mlflow_client():  # type: ignore[no-untyped-def]
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
    lines = ["| Model | LORO h=5 MAE | bias | WF h=5 MAE | LORO h=10 MAE | WF h=10 MAE |", "|---|---|---|---|---|---|"]
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
    lines = ["| Model | 1 · MAE ≤ 0.9 × persistence | 2 · \\|bias\\| ≤ 0.05 | 3 · WF ≤ persistence | 4 · wins majority | Passes |",
             "|---|---|---|---|---|---|"]
    mark = {True: "✅", False: "❌", None: "n/a"}
    for v in sorted(check(result.summary, result.by_race), key=lambda v: v.loro_mae):
        c = v.checks
        lines.append(f"| {v.model} | {mark[c['1_mae_10pct_below_persistence']]} {v.loro_mae:.3f} vs ≤ {v.loro_target:.3f} | "
                     f"{mark[c['2_bias_within_0.05']]} {v.bias:+.3f} | "
                     f"{mark[c['3_walk_forward_not_worse']]} {v.wf_mae:.3f} vs ≤ {v.wf_target:.3f} | "
                     f"{mark[c['4_wins_majority_of_races']]} {v.races_won}/{v.races_total} | {'**yes**' if v.passes else 'no'} |")
    return "\n".join(lines)


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
    models = [PersistenceModel()] + [make_tree_forecaster(k, identity=i) for k in FACTORIES for i in (False, True)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = evaluate(dev, models, splits=("loro", "walk_forward"), horizons=(5, 10), n_jobs=args.jobs)

    run_id = None
    if not args.no_mlflow:
        mlflow = mlflow_client()
        with mlflow.start_run(run_name=f"6d-defaults-{meta['hash'][:12]}") as run, tempfile.TemporaryDirectory() as tmp:
            run_id = run.info.run_id
            mlflow.set_tags({"dataset_hash": meta["hash"], "git_sha": git("rev-parse", "HEAD"), "git_dirty": str(bool(dirty)),
                             "fastf1_version": ",".join(meta["fastf1_versions"]), "phase": "6D", "stage": "defaults",
                             "holdout_races": ",".join(held)})
            mlflow.log_params({"development_races": dev["session_key"].nunique(), "holdout": ",".join(held),
                               "splits": "loro,walk_forward", "horizons": "5,10", "target": "lap_time - mean(last 3 laps)",
                               "hyperparameters": "library defaults"})
            for r in result.summary.itertuples():
                for metric in ("mae", "mae_se", "rmse", "bias", "bias_se", "slope_mae_vs_ref"):
                    v = getattr(r, metric)
                    if pd.notna(v):
                        mlflow.log_metric(f"{r.model}.{r.split}.h{r.horizon}.{metric}", float(v))
            for name in ("summary", "by_step", "by_compound", "by_race"):
                path = Path(tmp) / f"{name}.csv"
                getattr(result, name).to_csv(path, index=False)
                mlflow.log_artifact(str(path))
            (Path(tmp) / "comparison.md").write_text(comparison_table(result) + "\n\n" + verdict_table(result), encoding="utf-8")
            mlflow.log_artifact(str(Path(tmp) / "comparison.md"))
    print(f"development races: {dev['session_key'].nunique()} · held out (untouched): {', '.join(held)}"
          + (f" · MLflow run {run_id}" if run_id else ""))
    print("\n" + comparison_table(result) + "\n\n" + verdict_table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
