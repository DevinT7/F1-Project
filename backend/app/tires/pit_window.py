"""Pit-window heuristic interval_crossing_v1 (Phase 6E). Pure functions, no I/O, no model.

Inputs are forecast curves for consecutive laps after the current lap, already on the scale
the caller compares (fuel-adjusted when the model has a fuel term, raw otherwise; the caller
records which):

    threshold     = best_pace + X                (X default 1.0 s: a heuristic, not a team rule)
    earliest_lap  = first lap with hi(l) ≥ threshold   (pessimistic: the tyre is "gone" soonest)
    central_lap   = first lap with m(l)  ≥ threshold
    latest_lap    = first lap with lo(l) ≥ threshold   (optimistic)

Because lo ≤ m ≤ hi at every lap (enforced here), earliest ≤ central ≤ latest whenever they
exist, and a later one can only be missing if the earlier ones exist or are missing too. A curve
that never crosses inside the horizon gives null, never an invented lap.

Null window (with a reason) when the forecast is too uncertain to act on: the interval width
at the end of the horizon exceeds 2·X, or the window is wider than MAX_WINDOW_LAPS.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import numpy.typing as npt

METHOD = "interval_crossing_v1"
DEFAULT_THRESHOLD_S = 1.0
HORIZON_CAP_LAPS = 25
MAX_WINDOW_LAPS = 10
UNCERTAINTY_FACTOR = 2.0
HIGH_MAX_WIDTH, HIGH_MIN_CLEAN_LAPS, MEDIUM_MAX_WIDTH = 3, 10, 6

ASSUMPTIONS: tuple[str, ...] = (
    "ignores undercut and overcut",
    "ignores traffic",
    "ignores track position",
    "ignores safety-car probability",
    "ignores tyre availability (sets left)",
    "threshold is a heuristic (best pace + X seconds), not a team rule",
)

Confidence = Literal["high", "medium", "low"]
NO_CROSSING = "no crossing within horizon"
Curve = Sequence[float] | npt.NDArray[np.float64]
TOO_UNCERTAIN = "forecast_uncertainty_too_high"


@dataclass(frozen=True)
class PitWindow:
    earliest_lap: int | None
    central_lap: int | None
    latest_lap: int | None
    threshold_s: float
    confidence: Confidence
    method: str = METHOD
    assumptions: tuple[str, ...] = ASSUMPTIONS
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PitWindowResult:
    window: PitWindow | None
    reason: str | None                 # why window is None
    best_pace_s: float
    threshold_x_s: float               # the X used (threshold = best pace + X)
    current_lap: int
    horizon_end_lap: int | None
    interval_width_at_horizon_s: float | None
    notes: tuple[str, ...] = ()

    @property
    def remaining_competitive_laps(self) -> int | None:
        if self.window is None or self.window.central_lap is None:
            return None
        return self.window.central_lap - self.current_lap


def best_pace(lap_times: Sequence[float], n_fastest: int = 3) -> float:
    """Median of the n fastest laps (fewer if fewer exist). Raises on no finite lap."""
    values = np.sort(np.asarray([t for t in lap_times if math.isfinite(t)], dtype=float))
    if not len(values):
        raise ValueError("best_pace needs at least one finite lap time")
    return float(np.median(values[:n_fastest]))


def horizon_laps(current_lap: int, final_lap: int, cap: int = HORIZON_CAP_LAPS) -> list[int]:
    """Laps the curves must cover: current+1 … min(current+cap, final lap)."""
    return list(range(current_lap + 1, min(current_lap + cap, final_lap) + 1))


def _first(laps: Sequence[int], values: npt.NDArray[np.float64], threshold: float) -> int | None:
    hit = np.flatnonzero(values >= threshold)
    return int(laps[int(hit[0])]) if len(hit) else None


def confidence(width: int | None, clean_laps: int) -> Confidence:
    if width is None:
        return "low"
    if width <= HIGH_MAX_WIDTH and clean_laps >= HIGH_MIN_CLEAN_LAPS:
        return "high"
    return "medium" if width <= MEDIUM_MAX_WIDTH else "low"


def interval_crossing(
    laps: Sequence[int],
    mean: Curve,
    lo: Curve,
    hi: Curve,
    *,
    best_pace_s: float,
    current_lap: int,
    final_lap: int,
    clean_laps: int,
    threshold_s: float = DEFAULT_THRESHOLD_S,
) -> PitWindowResult:
    laps = [int(x) for x in laps]
    m, lo_a, hi_a = (np.asarray(v, dtype=float) for v in (mean, lo, hi))
    if not (len(laps) == len(m) == len(lo_a) == len(hi_a)):
        raise ValueError("laps, mean, lo and hi must have the same length")
    if laps and laps != list(range(current_lap + 1, current_lap + 1 + len(laps))):
        raise ValueError("curve laps must be consecutive from current_lap + 1")
    if not (np.isfinite(m).all() and np.isfinite(lo_a).all() and np.isfinite(hi_a).all()) or not math.isfinite(best_pace_s):
        raise ValueError("curves and best pace must be finite")
    if threshold_s <= 0:
        raise ValueError("threshold_s must be positive")
    keep = [i for i, lap in enumerate(laps) if lap <= final_lap]  # never a lap after the flag
    laps = [laps[i] for i in keep]
    m, lo_a, hi_a = m[keep], lo_a[keep], hi_a[keep]
    lo_a, hi_a = np.minimum(lo_a, m), np.maximum(hi_a, m)  # lo ≤ m ≤ hi ⇒ ordered crossings

    def result(window: PitWindow | None, reason: str | None, notes: tuple[str, ...] = ()) -> PitWindowResult:
        return PitWindowResult(window, reason, best_pace_s, threshold_s, current_lap, laps[-1] if laps else None,
                               float(hi_a[-1] - lo_a[-1]) if laps else None, notes)

    if not laps:
        return result(None, "no laps left in the race")
    threshold = best_pace_s + threshold_s
    if hi_a[-1] - lo_a[-1] > UNCERTAINTY_FACTOR * threshold_s:
        return result(None, TOO_UNCERTAIN, (f"interval width {hi_a[-1] - lo_a[-1]:.2f} s at lap {laps[-1]} "
                                           f"exceeds {UNCERTAINTY_FACTOR:g} × {threshold_s:g} s",))
    earliest, central, latest = (_first(laps, c, threshold) for c in (hi_a, m, lo_a))
    width = latest - earliest if latest is not None and earliest is not None else None
    if width is not None and width > MAX_WINDOW_LAPS:
        return result(None, TOO_UNCERTAIN, (f"window {earliest}–{latest} is wider than {MAX_WINDOW_LAPS} laps",))
    notes = (NO_CROSSING,) if latest is None else ()
    window = PitWindow(earliest, central, latest, threshold, confidence(width, clean_laps), notes=notes)
    return result(window, None, notes)
