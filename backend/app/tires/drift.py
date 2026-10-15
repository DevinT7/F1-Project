"""Model drift (Phase 9): how well the production tyre model forecasts races it has never seen.

After each race ingest (the ingest job, app/jobs/system.py) the served model replays that race exactly as
the evaluation does (forecast cuts every few laps, horizon 5; app/tires/evaluation.simulate_race) and the
race's MAE joins a rolling window (app/observability.JobMetrics, last DRIFT_WINDOW races). /metrics exposes
the window's step-weighted MAE next to the model's recorded walk-forward MAE; the TyreModelDrift alert
(monitoring/prometheus/alerts.yml) fires when the first exceeds the second by more than 25%.

Races at or before the model's `trained_through_session` are in its training data: skipped, never scored.
"""
from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import structlog

from ..storage.repository import SessionRepository
from .dataset import build_dataset
from .evaluation import simulate_race
from .registry import Registry

log = structlog.get_logger("raceiq.drift")
HORIZON = 5


def _order(session_key: str) -> tuple[int, int]:
    year, rnd, _ = session_key.split("_")
    return int(year), int(rnd)


def session_mae(repo: SessionRepository, session_key: str, model: Any) -> tuple[float, int] | None:
    """(MAE in seconds, forecast steps) of `model` replaying one stored race; None if nothing to forecast."""
    frame = build_dataset(repo, [session_key]).frame
    if frame.empty:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        records = simulate_race(model, frame, (HORIZON,))
    if records.empty:
        return None
    return float(np.mean(np.abs(records["pred"] - records["actual"]))), len(records)


def record_session(repo: SessionRepository, session_key: str, registry: Registry, sink: Any) -> dict[str, Any]:
    """Score one newly ingested session and add it to the drift window. Returns what happened (for the
    job result and logs); never raises for "nothing to score"."""
    if not session_key.endswith("_R"):
        return {"drift": "skipped", "reason": "not_a_race"}
    entry = registry.production()
    if _order(session_key) <= _order(entry.trained_through_session):
        return {"drift": "skipped", "reason": "in_training_data"}
    scored = session_mae(repo, session_key, registry.load(entry))
    if scored is None:
        return {"drift": "skipped", "reason": "no_forecastable_stints"}
    mae, n = scored
    sink.record_drift(session_key, mae, n)
    log.info("drift.recorded", session=session_key, model=entry.version, mae_s=round(mae, 4), steps=n)
    return {"drift": "recorded", "mae_s": round(mae, 4), "steps": n}
