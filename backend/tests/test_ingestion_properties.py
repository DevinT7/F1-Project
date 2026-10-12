"""Phase 1 — property tests: normalized telemetry invariants hold for any input."""
from __future__ import annotations

import numpy as np
import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from app.ingestion.normalize import normalize_telemetry

_times = st.one_of(st.none(), st.floats(min_value=0, max_value=200, allow_nan=False))
_speeds = st.one_of(st.none(), st.floats(min_value=0, max_value=380, allow_nan=False))
_distances = st.one_of(st.none(), st.floats(min_value=-10, max_value=8000, allow_nan=False))


@st.composite
def raw_telemetry(draw: st.DrawFn) -> pd.DataFrame:
    rows = draw(st.lists(st.tuples(_times, _speeds, _distances), max_size=60))
    with_distance = draw(st.booleans())
    frame = pd.DataFrame({
        "Time": pd.to_timedelta([np.nan if t is None else t for t, _, _ in rows], unit="s"),
        "Speed": [np.nan if s is None else s for _, s, _ in rows],
        "DRS": draw(st.lists(st.integers(0, 14), min_size=len(rows), max_size=len(rows))),
        "Brake": draw(st.lists(st.booleans(), min_size=len(rows), max_size=len(rows))),
    })
    if with_distance:
        frame["Distance"] = [np.nan if d is None else d for _, _, d in rows]
    return frame


@settings(max_examples=300, deadline=None)
@given(raw_telemetry())
def test_time_strictly_increasing_and_distance_non_decreasing(raw):
    frame = normalize_telemetry(raw)
    assert np.all(np.diff(frame.time_s) > 0)
    assert np.all(np.diff(frame.distance_m) >= 0)
    assert not np.isnan(frame.time_s).any()
    assert not np.isnan(frame.distance_m).any()
    assert len(frame) <= len(raw)


@settings(max_examples=100, deadline=None)
@given(raw_telemetry())
def test_drs_open_is_derived_from_code(raw):
    frame = normalize_telemetry(raw)
    assert frame.drs_open.tolist() == [code in (10, 12, 14) for code in frame.drs_code.tolist()]
