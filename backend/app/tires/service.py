"""Tyre prediction service (Phase 6E).

predict(session, driver, lap, horizon) sees ONLY laps ≤ lap, for every driver (the exclusion
rules need the field: neutralizations, red-flag restarts). It applies the 6A exclusion rules to
that cut, builds the 6A rows for the driver's current stint, requires ≥ min_clean_laps clean
laps, and forecasts lap+1 … lap+N from known quantities only: tyre age and fuel advance one per
lap; temperature is frozen at the cut. Nothing after `lap` is ever read: hindsight is the bug
this module exists to prevent (tests mutate every later lap and compare outputs).

Fields that cannot be computed are None and explained in `warnings`; nothing is zero-filled.
"""
from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd

from ..ingestion.errors import DriverNotFoundError, IngestionError, LapNotFoundError, SessionNotFoundError
from ..ingestion.models import SessionInfo
from ..storage.repository import SessionRepository
from . import pit_window as pw
from .config import WET_COMPOUNDS, ExclusionConfig
from .dataset import build_rows, laps_frame
from .exclusions import apply_rules
from .registry import ModelRegistry, RegistryEntry
from .serving import ServedModel

MAX_HORIZON = pw.HORIZON_CAP_LAPS


class NotARaceSessionError(IngestionError):
    """Tyre forecasting needs a race session (HTTP 422)."""


@dataclass(frozen=True)
class TireServiceConfig:
    min_clean_laps: int = 5
    threshold_s: float = pw.DEFAULT_THRESHOLD_S
    horizon_cap: int = pw.HORIZON_CAP_LAPS
    pit_window_gate: pw.Gate = "central"   # where the 2·X uncertainty rule is judged (see pit_window.py)
    cache_size: int = 512
    session_cache_size: int = 8


@dataclass(frozen=True)
class PredictedLap:
    lap: int
    pred_s: float
    lo_s: float
    hi_s: float


@dataclass(frozen=True)
class Prediction:
    """Service-level result; the API schema mirrors it field for field."""
    status: str                                  # "ok" | "insufficient_data"
    session_key: str
    driver: str
    lap: int
    horizon: int
    reason: str | None = None
    stint: int | None = None
    compound: str | None = None
    tyre_age: int | None = None
    clean_laps_used: int | None = None
    degradation_s_per_lap: float | None = None
    degradation_ci: tuple[float, float] | None = None
    predicted_laps: tuple[PredictedLap, ...] | None = None
    pit_window: dict[str, Any] | None = None
    pit_window_reason: str | None = None
    remaining_competitive_laps: int | None = None
    model: dict[str, str] | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Session:
    info: SessionInfo
    laps: pd.DataFrame          # every driver's laps (laps_frame), all laps of the race
    drivers: dict[str, str]     # upper-case code or number → code


class TireService:
    def __init__(self, repo: SessionRepository, registry: ModelRegistry,
                 config: TireServiceConfig = TireServiceConfig()) -> None:
        self.repo, self.registry, self.config = repo, registry, config
        self._cache: OrderedDict[tuple[Any, ...], Prediction] = OrderedDict()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- inputs
    def _session(self, key: str) -> _Session:
        with self._lock:
            hit = self._sessions.get(key)
            if hit is not None:
                self._sessions.move_to_end(key)
                return hit
        if not self.repo.has_session(key):
            raise SessionNotFoundError(f"Session {key.replace('_', '-')} is not ingested; POST .../ingest first.")
        info = self.repo.get_session_info(key)
        if info.session_code != "R":
            raise NotARaceSessionError(f"tire forecasting requires a race session; {key.replace('_', '-')} is "
                                       f"{info.session_name or info.session_code}")
        drivers: dict[str, str] = {}
        for d in self.repo.list_drivers(key):
            drivers[d.code.upper()] = d.code
            drivers[d.number] = d.code
        session = _Session(info, laps_frame(self.repo.list_laps(key)), drivers)
        with self._lock:
            self._sessions[key] = session
            while len(self._sessions) > self.config.session_cache_size:
                self._sessions.popitem(last=False)
        return session

    # ---------------------------------------------------------------- public
    def predict(self, session_key: str, driver: str, lap: int, horizon: int,
                threshold_s: float | None = None) -> Prediction:
        if not 1 <= horizon <= MAX_HORIZON:
            raise ValueError(f"horizon must be within 1…{MAX_HORIZON}")
        x = self.config.threshold_s if threshold_s is None else threshold_s
        session = self._session(session_key)
        code = session.drivers.get(driver.strip().upper())
        if code is None:
            raise DriverNotFoundError(f"Driver {driver} is not in session {session_key.replace('_', '-')}.")
        mine = session.laps[session.laps["driver"] == code]
        if lap not in set(mine["lap_number"].astype(int)):
            raise LapNotFoundError(f"{code} has no lap {lap} in {session_key.replace('_', '-')}.")
        entry = self.registry.production()          # ModelNotAvailableError → 503
        cache_key = (session_key, code, lap, horizon, x, entry.version, entry.sha256)
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit is not None:
                self._cache.move_to_end(cache_key)
                return hit
        model = self.registry.load(entry)
        result = self._predict(session, code, lap, horizon, x, model, entry)
        with self._lock:
            self._cache[cache_key] = result
            while len(self._cache) > self.config.cache_size:
                self._cache.popitem(last=False)
        return result

    # ---------------------------------------------------------------- the work
    def _predict(self, session: _Session, code: str, lap: int, horizon: int, x: float, model: ServedModel,
                 entry: RegistryEntry) -> Prediction:
        info = session.info
        base = Prediction(status="ok", session_key=info.key, driver=code, lap=lap, horizon=horizon,
                          model={"name": entry.name, "version": entry.version, "trained_through": entry.trained_through})
        cut = session.laps[session.laps["lap_number"] <= lap]          # THE cut: nothing after `lap`
        raw = cut[(cut["driver"] == code) & (cut["lap_number"] == lap)].iloc[0]
        stint_no, compound, age_now = raw["stint"], raw["compound"], raw["tyre_age"]
        if pd.isna(stint_no) or compound is None or pd.isna(age_now):
            return replace(base, status="insufficient_data",
                           reason="stint, compound or tyre age unknown at this lap (FastF1 timing gap)")
        stint_no, age_now, compound = int(stint_no), int(age_now), str(compound)
        base = replace(base, stint=stint_no, compound=compound, tyre_age=age_now)
        if compound in WET_COMPOUNDS:
            return replace(base, status="insufficient_data", reason=f"{compound} tyres are not modelled (dry races only)")
        if not model.point_model.supports(compound):
            return replace(base, status="insufficient_data", reason=f"model has no estimate for {compound}")
        total = int(info.n_laps_scheduled or session.laps["lap_number"].max())
        if lap >= total:
            return replace(base, status="insufficient_data", reason="no laps left in the race")

        cfg = ExclusionConfig(min_clean_stint_laps=self.config.min_clean_laps, keep_short_stints=True)
        rows = build_rows(info, apply_rules(cut, cfg).kept, cut)
        history = rows[(rows["driver"] == code) & (rows["stint"] == stint_no)].sort_values("lap_number")
        clean = len(history)
        base = replace(base, clean_laps_used=clean)
        if clean < self.config.min_clean_laps:
            return replace(base, status="insufficient_data",
                           reason=f"{clean} clean laps in this stint so far; at least {self.config.min_clean_laps} needed")
        warnings: list[str] = []
        if pd.notna(raw["pit_in_time_s"]):
            warnings.append(f"{code} entered the pits at the end of lap {lap}: the forecast assumes the stint continues")

        laps = pw.horizon_laps(lap, total, max(horizon, self.config.horizon_cap))
        last_temp = raw["track_temp_c"] if pd.notna(raw["track_temp_c"]) else history["track_temp_c"].iloc[-1]
        future = pd.DataFrame({
            "lap_number": laps, "compound": compound, "stint": stint_no,
            "tyre_age": [age_now + (n - lap) for n in laps], "fuel_proxy": [total - n for n in laps],
            "track_temp_c": float(last_temp), "lap_time_s": np.nan,
        })
        fc = model.forecast(history, future)
        if not (np.isfinite(fc.mean).all() and np.isfinite(fc.lo).all() and np.isfinite(fc.hi).all()):
            return replace(base, status="insufficient_data", reason="model produced a non-finite forecast")
        if fc.extrapolated_steps:
            warnings.append(f"interval for steps {fc.extrapolated_steps[0]}–{fc.extrapolated_steps[-1]} reuses the largest "
                            "calibrated step (no calibration data that far ahead)")
        predicted = tuple(PredictedLap(n, round(float(m), 3), round(float(lo), 3), round(float(hi), 3))
                          for n, m, lo, hi in zip(laps[:horizon], fc.mean, fc.lo, fc.hi, strict=False))
        if len(predicted) < horizon:
            warnings.append(f"only {len(predicted)} lap{'' if len(predicted) == 1 else 's'} remain{'s' if len(predicted) == 1 else ''} in the race; horizon shortened")

        deg = model.point_model.degradation(compound, float(age_now))
        degradation, ci = None, None
        if deg is None:
            warnings.append(f"degradation_s_per_lap: the {entry.kind} model has no degradation term")
        else:
            degradation = round(deg.value_s_per_lap, 4)
            if deg.ci_low is not None and deg.ci_high is not None:
                ci = (round(deg.ci_low, 4), round(deg.ci_high, 4))
            else:
                warnings.append("degradation_ci: not available for this model")

        # pit window on fuel-adjusted pace when the model has a fuel term (raw otherwise)
        fuel = model.point_model.fuel_coef_s_per_lap
        hist_fuel = history["fuel_proxy"].to_numpy(dtype=float)
        fut_fuel = future["fuel_proxy"].to_numpy(dtype=float)
        beta = fuel if fuel is not None and math.isfinite(fuel) else 0.0
        basis = "fuel_adjusted_model_term" if beta else "raw"
        adjusted = history["lap_time_s"].to_numpy(dtype=float) - beta * hist_fuel
        best = pw.best_pace(adjusted.tolist())
        recent = float(np.mean(adjusted[-3:]))  # same basis and window as the persistence anchor
        res = pw.interval_crossing(laps, fc.mean - beta * fut_fuel, fc.lo - beta * fut_fuel, fc.hi - beta * fut_fuel,
                                   best_pace_s=best, current_lap=lap, final_lap=total, clean_laps=clean, threshold_s=x,
                                   gate=self.config.pit_window_gate, current_pace_s=recent)
        window: dict[str, Any] | None = None
        reason = None
        if res.window is None:
            reason = res.reason
            warnings.append(f"pit_window: {res.reason}" + (f" ({'; '.join(res.notes)})" if res.notes else ""))
        else:
            w = res.window
            window = {"earliest_lap": w.earliest_lap, "central_lap": w.central_lap, "latest_lap": w.latest_lap,
                      "threshold_s": x, "threshold_pace_s": round(w.threshold_s, 3), "best_pace_s": round(best, 3),
                      "pace_basis": basis, "method": w.method, "confidence": w.confidence,
                      "assumptions": list(w.assumptions), "notes": list(w.notes)}
        remaining = res.remaining_competitive_laps
        if remaining is None and window is not None:
            warnings.append("remaining_competitive_laps: the forecast never reaches the threshold within the horizon")
        return replace(base, degradation_s_per_lap=degradation, degradation_ci=ci, predicted_laps=predicted,
                       pit_window=window, pit_window_reason=reason, remaining_competitive_laps=remaining,
                       warnings=tuple(warnings))

    def models(self) -> list[RegistryEntry]:
        return self.registry.entries()
