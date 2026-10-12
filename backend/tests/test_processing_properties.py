"""Phase 2 — property tests: delta continuity and shape for arbitrary sampling."""
from __future__ import annotations

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from app.processing.delta import compare_laps
from tests.fixtures import synthetic as syn

REF_LAP = syn.sample_lap(syn.make_profile(), hz=10)


@settings(max_examples=40, deadline=None)
@given(
    hz=st.floats(4.0, 30.0),
    jitter=st.floats(0.0, 0.3),
    drift=st.floats(-0.025, 0.025),
    slow_factor=st.sampled_from([0.85, 0.9, 0.95, 1.0]),
    zone_start=st.sampled_from([200.0, 900.0, 2340.0, 3900.0]),
    seed=st.integers(0, 10_000),
)
def test_delta_is_continuous_and_grid_shaped(hz, jitter, drift, slow_factor, zone_start, seed):
    profile = syn.make_profile(slow_zone=(zone_start, zone_start + 250.0), slow_factor=slow_factor)
    target = syn.sample_lap(profile, hz=hz, jitter_s=jitter / hz, drift=drift, seed=seed)
    cmp = compare_laps(REF_LAP, target)
    n = len(cmp.grid_m)
    assert all(len(a) == n for a in (cmp.delta_s, cmp.speed_diff_kph, cmp.target.time_s, cmp.target.gear))
    # 0.05 s per metre would be absurd (a 20 m/s car loses at most ~0.05 s/m if it stops dead)
    assert np.max(np.abs(np.diff(cmp.delta_s)) / np.diff(cmp.grid_m)) < 0.05
    assert abs(cmp.quality.final_delta_error_s) <= 0.02
    assert np.all(np.diff(cmp.target.time_s) > 0)
    truth = profile.time_at(cmp.grid_m) - syn.make_profile().time_at(cmp.grid_m)
    assert np.abs(cmp.delta_s - truth).max() < 0.01
