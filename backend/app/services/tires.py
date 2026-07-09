"""
Tire-degradation analysis (Person 3).

Ships a **baseline** that works today: fit a line to a stint's clean lap times vs
tyre age → degradation rate (s/lap), extrapolate future pace, estimate a pit window
and a confidence band from the fit residuals.

When the trained ML model (ml/train.py → model artifact) is available, load it here and
replace `_baseline_fit`. The endpoint shape (TireAnalysis) stays identical, so the
frontend never changes.
"""
from __future__ import annotations

import numpy as np

from ..clients.openf1 import get_client
from ..models.schemas import LapPacePrediction, TireAnalysis

# pace within this many seconds of the stint's best is still "competitive"
COMPETITIVE_THRESHOLD_S = 1.2


async def analyse_current_stint(session_key: int | str, driver_number: int,
                                horizon: int = 10) -> TireAnalysis:
    laps = await get_client().laps(session_key=session_key, driver_number=driver_number)
    stints = await get_client().stints(session_key=session_key, driver_number=driver_number)
    if not stints:
        return _empty(session_key, driver_number)

    stint = max(stints, key=lambda s: s.get("stint_number", 0))  # current/last stint
    lap_start = stint.get("lap_start", 1)
    lap_end = stint.get("lap_end", 10**6)
    age0 = stint.get("tyre_age_at_start", 0)

    clean = [
        l for l in laps
        if l.get("lap_duration") and not l.get("is_pit_out_lap")
        and lap_start <= l.get("lap_number", -1) <= lap_end
    ]
    if len(clean) < 3:
        return _empty(session_key, driver_number, compound=stint.get("compound"),
                      tyre_age=age0)

    ages = np.array([age0 + (l["lap_number"] - lap_start) for l in clean], dtype=float)
    times = np.array([l["lap_duration"] for l in clean], dtype=float)
    times = _remove_outliers(ages, times)

    slope, intercept, resid_std = _baseline_fit(ages, times)
    current_age = int(ages.max())
    best = float(np.nanmin(times))

    preds: list[LapPacePrediction] = []
    for k in range(0, horizon + 1):
        age = current_age + k
        mu = slope * age + intercept
        band = 1.96 * resid_std + 0.01 * k  # widen with horizon
        preds.append(LapPacePrediction(
            laps_ahead=k,
            predicted_lap_time_s=round(mu, 3),
            lower_s=round(mu - band, 3), upper_s=round(mu + band, 3)))

    remaining = _remaining_competitive(slope, intercept, current_age, best)
    pit_window = (current_age + max(1, remaining - 2), current_age + remaining + 2) \
        if remaining is not None else None
    confidence = _confidence(resid_std, slope)

    return TireAnalysis(
        session_key=int(session_key), driver_number=driver_number,
        compound=stint.get("compound"), tyre_age_laps=current_age,
        degradation_s_per_lap=round(float(slope), 3),
        remaining_competitive_laps=remaining,
        recommended_pit_window=pit_window,
        model_confidence=confidence, predictions=preds,
        model_name="baseline-linear",
    )


def _baseline_fit(ages: np.ndarray, times: np.ndarray) -> tuple[float, float, float]:
    slope, intercept = np.polyfit(ages, times, 1)
    resid = times - (slope * ages + intercept)
    return float(slope), float(intercept), float(np.std(resid) or 0.05)


def _remove_outliers(ages: np.ndarray, times: np.ndarray) -> np.ndarray:
    """Blank out safety-car / traffic laps (much slower than the local trend)."""
    med = np.median(times)
    mad = np.median(np.abs(times - med)) or 0.1
    keep = np.abs(times - med) < 5 * mad
    out = times.copy()
    out[~keep] = np.interp(ages[~keep], ages[keep], times[keep]) if keep.any() else times[~keep]
    return out


def _remaining_competitive(slope: float, intercept: float, current_age: int,
                           best: float) -> int | None:
    if slope <= 0:
        return None  # tyres not degrading (or warming up) — no window yet
    for k in range(0, 40):
        if slope * (current_age + k) + intercept > best + COMPETITIVE_THRESHOLD_S:
            return k
    return 40


def _confidence(resid_std: float, slope: float) -> float:
    # tighter residuals + clearer trend => more confidence
    base = 1.0 / (1.0 + resid_std * 3.0)
    return round(float(np.clip(base, 0.4, 0.97)), 2)


def _empty(session_key, driver_number, compound=None, tyre_age=None) -> TireAnalysis:
    return TireAnalysis(
        session_key=int(session_key), driver_number=driver_number,
        compound=compound, tyre_age_laps=tyre_age,
        degradation_s_per_lap=0.0, remaining_competitive_laps=None,
        recommended_pit_window=None, model_confidence=0.4, predictions=[],
        model_name="baseline-linear",
    )
