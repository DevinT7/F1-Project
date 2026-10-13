"""Saving the shipped tyre model: a pickle plus a metadata JSON (dataset hash, git SHA, features,
hyperparameters, interval calibration). The service in a later phase loads both and refuses a
model whose dataset hash does not match the data it serves."""
from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

from .features import BASE_CATEGORICAL, IDENTITY_CATEGORICAL, NUMERIC_FEATURES


def model_metadata(model: Any, *, dataset_hash: str, git_sha: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": model.name, "params": {k: v for k, v in model.params().items()},
        "dataset_hash": dataset_hash, "git_sha": git_sha,
        "target": "lap_time_s - mean(last 3 history laps)",
        "numeric_features": list(NUMERIC_FEATURES), "categorical_features": list(BASE_CATEGORICAL),
        "identity_features": list(IDENTITY_CATEGORICAL),
    }
    for attr in ("alpha", "calibration_fraction"):
        if hasattr(model, attr):
            meta[attr] = getattr(model, attr)
    if hasattr(model, "step_quantiles"):
        meta["conformal_step_quantiles"] = {str(k): v for k, v in model.step_quantiles.items()}
        meta["calibration_residuals"] = {str(k): v for k, v in model.residuals.items()}
    meta.update(extra or {})
    return meta


def save(model: Any, out_dir: Path, *, dataset_hash: str, git_sha: str,
         extra: dict[str, Any] | None = None) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"tire_model_{dataset_hash[:12]}_{git_sha[:10]}"
    pkl, js = out_dir / f"{stem}.pkl", out_dir / f"{stem}.json"
    pkl.write_bytes(pickle.dumps(model))
    js.write_text(json.dumps(model_metadata(model, dataset_hash=dataset_hash, git_sha=git_sha, extra=extra),
                             indent=2, default=str), encoding="utf-8")
    return pkl, js


def load(pkl: Path, *, expected_hash: str | None = None) -> tuple[Any, dict[str, Any]]:
    meta = json.loads(pkl.with_suffix(".json").read_text(encoding="utf-8"))
    if expected_hash is not None and meta["dataset_hash"] != expected_hash:
        raise ValueError(f"model trained on dataset {meta['dataset_hash'][:12]}, not {expected_hash[:12]}")
    return pickle.loads(pkl.read_bytes()), meta
