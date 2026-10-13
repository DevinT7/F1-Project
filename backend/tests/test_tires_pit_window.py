"""Phase 6E — interval_crossing_v1 on hand-computed curves.

Slopes and bands are multiples of 1/8 so every crossing is exact in binary floating point:
current lap 10, best pace 90.0 s, X = 1.0 s ⇒ threshold 91.0 s; k = laps after the cut.
"""
from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.tires import pit_window as P

CURRENT, FINAL, BEST = 10, 60, 90.0
LAPS = P.horizon_laps(CURRENT, FINAL)  # 11 … 35


def curve(slope: float, start: float = BEST) -> np.ndarray:
    return start + slope * (np.asarray(LAPS) - CURRENT)


def run(m, band_lo, band_hi=None, *, clean=12, final=FINAL, laps=None):
    band_hi = band_lo if band_hi is None else band_hi
    laps = LAPS if laps is None else laps
    return P.interval_crossing(laps, m, m - band_lo, m + band_hi, best_pace_s=BEST, current_lap=CURRENT,
                               final_lap=final, clean_laps=clean)


def test_a_linear_curve_crosses_exactly_at_the_known_lap():
    # m = 90 + 0.25·k reaches 91.0 at k = 4 → lap 14; a zero-width band puts all three there
    r = run(curve(0.25), 0.0)
    w = r.window
    assert w is not None and (w.earliest_lap, w.central_lap, w.latest_lap) == (14, 14, 14)
    assert w.confidence == "high" and w.threshold_s == 91.0 and r.remaining_competitive_laps == 4
    assert w.method == "interval_crossing_v1" and "ignores undercut and overcut" in w.assumptions


def test_b_wide_interval_gives_earliest_before_central_before_latest():
    # hi = 90.5 + 0.25k → k=2 (lap 12); m → lap 14; lo = 89.5 + 0.25k → k=6 (lap 16)
    w = run(curve(0.25), 0.5).window
    assert w is not None and (w.earliest_lap, w.central_lap, w.latest_lap) == (12, 14, 16)
    assert w.confidence == "medium"  # width 4: ≤ 6 but > 3


def test_c_never_crossing_returns_null_not_a_fabricated_lap():
    flat = run(curve(0.0, 90.25), 0.25)
    assert flat.window is not None and (flat.window.earliest_lap, flat.window.central_lap, flat.window.latest_lap) == (None, None, None)
    assert P.NO_CROSSING in flat.window.notes and flat.remaining_competitive_laps is None
    # partial: hi = 90.5 + 0.05k crosses at k=10 (lap 20), m at k=20 (lap 30), lo never (max 90.75)
    part = run(curve(0.05), 0.5).window
    assert part is not None and (part.earliest_lap, part.central_lap, part.latest_lap) == (20, 30, None)
    assert part.confidence == "low" and P.NO_CROSSING in part.notes


def test_d_uncertainty_too_high_returns_a_null_window_with_a_reason():
    # interval width at the horizon 2.25 s > 2 × X
    r = run(curve(0.25), 1.125)
    assert r.window is None and r.reason == P.TOO_UNCERTAIN and r.interval_width_at_horizon_s == 2.25
    # window 12 … 24 is 12 laps wide (> 10) although the interval itself (1.5 s) is acceptable:
    # m = 90 + 0.125k → lap 18; hi = 90.75 + 0.125k → lap 12; lo = 89.25 + 0.125k → lap 24
    r = run(curve(0.125), 0.75)
    assert r.window is None and r.reason == P.TOO_UNCERTAIN


@settings(max_examples=300, deadline=None)
@given(
    slope=st.floats(-0.2, 0.3), start=st.floats(89.0, 92.0),
    lo_w=st.lists(st.floats(0, 1.5), min_size=25, max_size=25),
    hi_w=st.lists(st.floats(0, 1.5), min_size=25, max_size=25),
    noise=st.lists(st.floats(-0.3, 0.3), min_size=25, max_size=25),
    clean=st.integers(5, 30),
)
def test_e_ordering_invariant(slope, start, lo_w, hi_w, noise, clean):
    m = curve(slope, start) + np.asarray(noise)
    r = P.interval_crossing(LAPS, m, m - np.asarray(lo_w), m + np.asarray(hi_w), best_pace_s=BEST,
                            current_lap=CURRENT, final_lap=FINAL, clean_laps=clean)
    if r.window is None:
        assert r.reason == P.TOO_UNCERTAIN
        return
    e, c, latest = r.window.earliest_lap, r.window.central_lap, r.window.latest_lap
    defined = [x for x in (e, c, latest) if x is not None]
    assert defined == sorted(defined)
    assert (c is None or e is not None) and (latest is None or c is not None)  # a later one needs the earlier
    assert all(CURRENT < x <= FINAL for x in defined)


def test_window_is_capped_at_the_final_race_lap():
    laps = P.horizon_laps(CURRENT, 13)
    assert laps == [11, 12, 13]
    m = BEST + 0.25 * (np.asarray(laps) - CURRENT)
    w = P.interval_crossing(laps, m, m, m, best_pace_s=BEST, current_lap=CURRENT, final_lap=13, clean_laps=12).window
    assert w is not None and w.central_lap is None and P.NO_CROSSING in w.notes  # would cross at 14: after the flag


def test_inputs_are_validated():
    m = curve(0.25)
    with pytest.raises(ValueError):
        P.interval_crossing(LAPS[1:], m[1:], m[1:], m[1:], best_pace_s=BEST, current_lap=CURRENT, final_lap=FINAL,
                            clean_laps=12)  # not consecutive from current + 1
    with pytest.raises(ValueError):
        P.interval_crossing(LAPS, m, m, m + np.nan, best_pace_s=BEST, current_lap=CURRENT, final_lap=FINAL, clean_laps=12)


def test_best_pace_is_the_median_of_the_three_fastest():
    assert P.best_pace([92.0, 90.4, 91.0, 90.0, 90.2, float("nan")]) == 90.2
    assert P.best_pace([91.0]) == 91.0
    with pytest.raises(ValueError):
        P.best_pace([])
