"""Build the servable tyre models and models/registry.json (Phase 6E).

    cd backend && python -m scripts.build_tire_registry [--production pooled_fixed_effects] [--allow-dirty]

For each servable kind (Baseline B, persistence): split-conformal at α = 0.2 on the full 6A
dataset. The point model is fitted on the earliest 70% of races and the most recent 30% are
the calibration races (as in 6D), with horizons up to 25 laps so every pit-window step has a
calibrated quantile. Writes models/tires/<name>/<version>/{model.json, residuals.npz} and the
registry with SHA-256 checksums, the dataset hash, git SHA and the 6D metrics summary.

The 6D protocol shipped Baseline B; persistence is registered as a candidate (6D report
recommendation, pending review). Switch with --production persistence.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import pickle
import sys
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.config import settings
from app.storage.repository import ParquetRepository
from app.tires.evaluation import race_order
from app.tires.models.base import StintContext
from app.tires.models.baseline_fixed import BaselineFixed
from app.tires.models.forecasting import PooledFixedEffects, _temp_coef
from app.tires.models.intervals import SplitConformal
from app.tires.models.persistence import PersistenceModel
from app.tires.registry import RegistryEntry, RegistryFile, sha256_file
from app.tires.report import latest_outputs
from app.tires.serving import FORMAT, FixedEffectsPoint, PersistencePoint, PointModel, point_model_to_json, residuals_to_npz
from scripts.run_eval import REPO, git

ALPHA = 0.2
HORIZONS = (5, 10, 25)
REGISTRY = REPO / "models" / "registry.json"
NAMES = {"pooled_fixed_effects": "baseline_b_conformal", "persistence": "persistence_conformal"}
EVAL_NAMES = {"pooled_fixed_effects": "pooled_fixed_effects+conformal80", "persistence": "persistence+conformal80"}


def fixed_effects_point(model: PooledFixedEffects) -> FixedEffectsPoint:
    fit = model.fit_
    assert fit is not None
    helper = BaselineFixed(pooled=True)
    helper.fits = {"pooled": fit}
    ci: dict[str, tuple[float, float]] = {}
    for c in fit.compounds:
        est = helper.degradation_per_lap(StintContext("pooled", "-", 0, c, 10.0, pd.DataFrame()))
        if est.ci_low is not None and est.ci_high is not None:
            ci[c] = (est.ci_low, est.ci_high)
    return FixedEffectsPoint(beta_fuel=fit.beta_fuel, beta_age=dict(fit.beta_age), gamma=dict(fit.gamma),
                             beta_temp=_temp_coef(fit), degradation_ci=ci)


def metrics_6d(kind: str) -> dict[str, Any]:
    """The 6D evaluation of the same configuration (holdout once; walk-forward on development races)."""
    out: dict[str, Any] = {}
    name = EVAL_NAMES[kind]
    for stage, split in (("final", "holdout"), ("intervals", "walk_forward")):
        files = sorted(glob.glob(str(REPO / "data" / "tires" / "eval" / f"6d_{stage}_*.pkl")))
        if not files:
            continue
        s = pickle.loads(Path(files[-1]).read_bytes()).summary  # our own evaluation output
        for h in (5, 10):
            row = s[(s["model"] == name) & (s["split"] == split) & (s["horizon"] == h)]
            if len(row):
                r = row.iloc[0]
                for col in ("mae", "bias", "coverage", "mean_width"):
                    if pd.notna(r[col]):
                        out[f"{split}_h{h}_{col}"] = round(float(r[col]), 4)
                out[f"{split}_n_races"] = int(r["n_races"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.build_tire_registry")
    parser.add_argument("--production", choices=list(NAMES), default="pooled_fixed_effects")
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    dirty = git("status", "--porcelain")
    if dirty and not args.allow_dirty:
        print("refusing to build: the git tree is dirty (commit first, or pass --allow-dirty)", file=sys.stderr)
        return 2
    sha = git("rev-parse", "HEAD")
    frame, _log, meta = latest_outputs(Path(args.data_dir) / "tires", None)
    order = race_order(frame)
    last = order[-1]
    trained_through = ParquetRepository(args.data_dir).get_session_info(last).date.date().isoformat()
    version = f"{trained_through}.{meta['hash'][:8]}"

    entries: list[RegistryEntry] = []
    for kind, name in NAMES.items():
        base = PooledFixedEffects() if kind == "pooled_fixed_effects" else PersistenceModel()
        conformal = SplitConformal(base, alpha=ALPHA, horizons=HORIZONS)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            conformal.fit(frame)
        point: PointModel = fixed_effects_point(base) if isinstance(base, PooledFixedEffects) else PersistencePoint()
        residuals = {s: np.asarray(r, dtype=np.float64) for s, r in conformal.residuals.items()}
        n_cal = max(1, int(round(len(order) * conformal.calibration_fraction)))
        out_dir = REGISTRY.parent / "tires" / name / version
        out_dir.mkdir(parents=True, exist_ok=True)
        spec = {"format": FORMAT, "name": name, "version": version, "point_model": point_model_to_json(point),
                "interval": {"method": "split-conformal", "alpha": ALPHA, "horizons_calibrated": list(HORIZONS),
                             "fitted_on": order[:-n_cal], "calibrated_on": order[-n_cal:]}}
        (out_dir / "model.json").write_text(json.dumps(spec, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (out_dir / "residuals.npz").write_bytes(residuals_to_npz(residuals))
        rel = out_dir.relative_to(REGISTRY.parent).as_posix()
        entries.append(RegistryEntry(
            name=name, version=version, kind=kind, status="production" if kind == args.production else "candidate",
            artifact=f"{rel}/model.json", sha256=sha256_file(out_dir / "model.json"),
            calibration_residuals=f"{rel}/residuals.npz", calibration_sha256=sha256_file(out_dir / "residuals.npz"),
            dataset_hash=meta["hash"], git_sha=sha, trained_through=trained_through, trained_through_session=last,
            alpha=ALPHA, metrics=metrics_6d(kind),
            notes=("6D protocol fallback (no model met the bar)" if kind == "pooled_fixed_effects"
                   else "6D report recommendation: most accurate, 80% holdout coverage in band"),
        ))
        q = {s: round(float(np.quantile(r, 1 - ALPHA)), 3) for s, r in residuals.items() if s in (1, 5, 10, 25)}
        print(f"{name} [{entries[-1].status}] {version}: residual quantiles {q}", flush=True)
    REGISTRY.write_text(RegistryFile(models=entries).model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {REGISTRY}")
    assert all(math.isfinite(v) for e in entries for v in e.metrics.values() if isinstance(v, float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
