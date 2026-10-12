"""Phase 2 — lap alignment and delta engine against laps with a known answer.

Synthetic laps come from tests/fixtures/synthetic.py: the exact t(d) of every profile
is known, so the grid delta is checked against the truth, not just for plausibility.
"""
from __future__ import annotations

import numpy as np
import pytest

from app.ingestion.models import TELEMETRY_COLUMNS, TelemetryFrame
from app.processing.alignment import clean_lap, make_grid, normalize_length, resample_to_grid
from app.processing.delta import compare_laps, compute_delta, sector_check
from app.processing.errors import InsufficientDataError, LapInvalidError
from app.processing.models import LapData
from tests.fixtures import synthetic as syn

REF = syn.make_profile()
T = REF.lap_time_s


def _true_delta(target_profile: syn.Profile, grid: np.ndarray) -> np.ndarray:
    """Exact delta on the reference's distance axis (both profiles share track length)."""
    return target_profile.time_at(grid) - REF.time_at(grid)


def _with_columns(lap: LapData, **changes: np.ndarray) -> LapData:
    cols = {**lap.telemetry.columns(), **changes}
    return LapData(TelemetryFrame.from_columns(cols), lap.lap_time_s, lap.driver, lap.lap_number, lap.sector_s)


def _rows(lap: LapData, index: np.ndarray) -> LapData:
    cols = {name: arr[index] for name, arr in lap.telemetry.columns().items()}
    return LapData(TelemetryFrame.from_columns(cols), lap.lap_time_s, lap.driver, lap.lap_number, lap.sector_s)


# ---------------------------------------------------------------- generator sanity
def test_generator_lap_is_realistic():
    assert 4700 < REF.length_m < 5100 and 60 < T < 90
    lap = syn.sample_lap(REF)
    assert lap.telemetry.time_s[-1] == T and lap.lap_time_s == T
    assert len(lap.telemetry) > 600


# ---------------------------------------------------------------- known answers
def test_uniform_0p2s_offset():
    slow = syn.make_profile(speed_scale=T / (T + 0.2))
    assert slow.lap_time_s == pytest.approx(T + 0.2, abs=1e-9)
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(slow, hz=8, seed=3))
    assert cmp.delta_s[0] == 0.0
    assert cmp.delta_s[-1] == pytest.approx(0.2, abs=1e-3)
    np.testing.assert_allclose(cmp.delta_s, _true_delta(slow, cmp.grid_m), atol=2e-3)
    # uniformly slower: loses time everywhere (checked every 50 m; 1 m steps carry ~1e-4 s interp noise)
    assert np.all(np.diff(cmp.delta_s[::50]) > 0)


def test_loss_concentrated_in_one_corner():
    lo, hi = syn.HAIRPIN_ZONE
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9)
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(slow, hz=12, seed=1))
    final = slow.lap_time_s - T
    assert 0.1 < final < 1.0
    g, delta = cmp.grid_m, cmp.delta_s
    assert np.abs(delta[g < lo - 5]).max() < 1e-3                 # flat (zero) before
    assert np.abs(delta[g > hi + 5] - final).max() < 1e-3         # flat (= final) after
    inside = delta[(g >= lo) & (g <= hi)]
    assert np.all(np.diff(inside[::25]) > 0) and inside[-1] - inside[0] > 0.9 * final  # ramps inside
    np.testing.assert_allclose(delta, _true_delta(slow, g), atol=2e-3)
    # display-only smoothed rate: positive in the zone, ~0 outside
    rate = cmp.delta_rate_smoothed
    assert rate[(g > lo + 50) & (g < hi - 50)].mean() > 0
    assert np.abs(rate[g < lo - 50]).max() < 1e-4


def test_distance_drift_is_normalized_away():
    # The classic bug: an un-normalized 2 % longer trace invents time loss late in the lap.
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9)
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(slow, drift=0.02, seed=5))
    assert cmp.quality.length_scale_factor == pytest.approx(1 / 1.02)
    np.testing.assert_allclose(cmp.delta_s, _true_delta(slow, cmp.grid_m), atol=3e-3)
    assert cmp.target.length_scale_factor == pytest.approx(1 / 1.02)
    assert cmp.reference.length_scale_factor == 1.0


def test_identity():
    lap = syn.sample_lap(REF, hz=9, jitter_s=0.02, seed=4)
    cmp = compare_laps(lap, lap)
    assert np.all(cmp.delta_s == 0.0)
    assert np.all(cmp.speed_diff_kph == 0.0)
    assert cmp.quality.final_delta_error_s == 0.0


def test_antisymmetry():
    a = syn.sample_lap(REF, hz=10, seed=1)
    b = syn.sample_lap(syn.make_profile(slow_zone=(800, 1100), slow_factor=0.93), hz=7, jitter_s=0.02, seed=2)
    ab, ba = compare_laps(a, b), compare_laps(b, a)
    np.testing.assert_array_equal(ab.grid_m, ba.grid_m)
    np.testing.assert_allclose(ab.delta_s, -ba.delta_s, atol=1e-6)
    np.testing.assert_allclose(ab.speed_diff_kph, -ba.speed_diff_kph, atol=1e-9)


def test_resampling_invariance():
    reference = syn.sample_lap(REF, hz=20)
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.95)
    # 10 Hz is roughly FastF1 merged telemetry. At 4 Hz samples are ~21 m apart at 315 km/h and
    # linear interpolation through a 315-to-95 km/h braking zone is off by up to ~2.5 km/h: a
    # sampling limit, not an alignment error (see docs/progress.md).
    coarse = compare_laps(reference, syn.sample_lap(slow, hz=10, jitter_s=0.01, seed=7)).target
    fine = compare_laps(reference, syn.sample_lap(slow, hz=25, seed=8)).target
    assert np.abs(coarse.speed_kph - fine.speed_kph).max() < 0.5
    assert np.abs(coarse.time_s - fine.time_s).max() < 0.005


def test_output_shapes_and_channels():
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, hz=6))
    n = len(cmp.grid_m)
    for arr in (cmp.delta_s, cmp.delta_rate, cmp.delta_rate_smoothed, cmp.speed_diff_kph,
                cmp.reference.time_s, cmp.target.speed_kph, cmp.target.gear, cmp.target.x):
        assert len(arr) == n
    np.testing.assert_allclose(cmp.delta_rate, np.gradient(cmp.delta_s, cmp.grid_m))
    np.testing.assert_allclose(cmp.speed_diff_kph, cmp.target.speed_kph - cmp.reference.speed_kph)
    assert cmp.step_m == 1.0
    assert np.all(np.diff(cmp.reference.time_s) > 0) and np.all(np.diff(cmp.target.time_s) > 0)
    q = cmp.quality
    assert q.sample_count_ref == len(syn.sample_lap(REF).telemetry)
    assert q.sample_count_target == len(syn.sample_lap(REF, hz=6).telemetry)


def test_discrete_channels_stay_discrete():
    lap = syn.sample_lap(REF, hz=5, seed=2)
    cmp = compare_laps(syn.sample_lap(REF), lap)
    tgt = cmp.target
    assert tgt.gear.dtype == np.int64 and set(np.unique(tgt.gear)) <= set(np.unique(lap.telemetry.gear))
    assert tgt.brake.dtype == np.bool_ and tgt.drs_open.dtype == np.bool_
    # step (previous-sample) interpolation: every grid gear equals the last sample at or before it
    d, gear = lap.telemetry.distance_m, lap.telemetry.gear
    idx = np.searchsorted(d, cmp.grid_m, side="right") - 1
    np.testing.assert_array_equal(tgt.gear, gear[np.clip(idx, 0, len(d) - 1)])


def test_step_size_is_configurable():
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, hz=8), step=5.0)
    assert cmp.step_m == 5.0 and cmp.grid_m[-1] == pytest.approx(REF.length_m)


# ---------------------------------------------------------------- grid / building blocks
def test_make_grid_ends_exactly_on_length():
    grid = make_grid(5000.3, 1.0)
    assert grid[0] == 0.0 and grid[-1] == 5000.3 and len(grid) == 5002
    assert np.all(np.diff(grid) > 0)
    near = make_grid(5000.0000001, 1.0)  # no sliver interval at the end
    assert near[-1] == 5000.0000001 and np.diff(near).min() > 0.5
    with pytest.raises(ValueError):
        make_grid(100.0, 0.0)


def test_clean_lap_reanchors_time_and_distance():
    lap = syn.sample_lap(REF)
    shifted = _with_columns(lap, time_s=lap.telemetry.time_s + 3600.0, distance_m=lap.telemetry.distance_m + 12.5)
    clean = clean_lap(shifted)
    assert clean.time_s[0] == 0.0 and clean.distance_m[0] == 0.0
    assert clean.duration_s == pytest.approx(T) and clean.length_m == pytest.approx(REF.length_m)


def test_normalize_length_scales_target():
    clean = clean_lap(syn.sample_lap(REF, drift=0.01))
    scaled = normalize_length(clean, REF.length_m)
    assert scaled.length_m == pytest.approx(REF.length_m)
    assert scaled.length_scale_factor == pytest.approx(1 / 1.01)
    aligned = resample_to_grid(scaled, make_grid(REF.length_m))
    assert aligned.length_scale_factor == pytest.approx(1 / 1.01)


def test_compute_delta_rejects_mismatched_grids():
    a = resample_to_grid(clean_lap(syn.sample_lap(REF)), make_grid(REF.length_m))
    b = resample_to_grid(clean_lap(syn.sample_lap(REF)), make_grid(REF.length_m, 2.0))
    with pytest.raises(ValueError):
        compute_delta(a, b)


# ---------------------------------------------------------------- failure modes
def test_truncated_lap_raises():
    lap = syn.sample_lap(REF)
    keep = np.arange(int(len(lap.telemetry) * 0.6))
    with pytest.raises(LapInvalidError, match="duration"):
        compare_laps(syn.sample_lap(REF), _rows(lap, keep))


def test_too_few_samples_raises():
    with pytest.raises(LapInvalidError, match="samples"):
        clean_lap(syn.sample_lap(REF, hz=2))


def test_duplicated_timestamps_are_dropped_not_double_counted():
    lap = syn.sample_lap(REF, hz=6, seed=3)
    n = len(lap.telemetry)
    doubled = _rows(lap, np.sort(np.concatenate([np.arange(n), np.arange(n)])))
    clean = clean_lap(doubled)
    assert clean.sample_count == n
    a, b = compare_laps(syn.sample_lap(REF), lap), compare_laps(syn.sample_lap(REF), doubled)
    np.testing.assert_array_equal(a.delta_s, b.delta_s)


def test_duplicates_leaving_too_few_samples_raise():
    lap = syn.sample_lap(REF, hz=2.2)  # ~188 unique samples
    n = len(lap.telemetry)
    assert n < 200
    with pytest.raises(LapInvalidError, match="samples"):
        clean_lap(_rows(lap, np.sort(np.concatenate([np.arange(n)] * 3))))


def test_unsorted_samples_are_sorted():
    lap = syn.sample_lap(REF, hz=6)
    rng = np.random.default_rng(0)
    shuffled = _rows(lap, rng.permutation(len(lap.telemetry)))
    np.testing.assert_array_equal(clean_lap(shuffled).time_s, clean_lap(lap).time_s)


def test_non_monotonic_distance_glitch_is_dropped():
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9)
    lap = syn.sample_lap(slow, hz=10, seed=6)
    d = lap.telemetry.distance_m.copy()
    d[300] = d[295]            # jumps back while at speed
    d[301:304] = d[300] - 1.0  # and stays behind for a few samples
    glitched = _with_columns(lap, distance_m=d)
    clean = clean_lap(glitched)
    assert clean.dropped_glitches == 4
    assert np.all(np.diff(clean.distance_m) > 0)
    cmp = compare_laps(syn.sample_lap(REF), glitched)
    np.testing.assert_allclose(cmp.delta_s, _true_delta(slow, cmp.grid_m), atol=3e-3)


def test_stationary_samples_are_repaired_with_epsilon():
    lap = syn.sample_lap(REF, hz=10)
    d, v = lap.telemetry.distance_m.copy(), lap.telemetry.speed_kph.copy()
    d[500] = d[499]
    v[499] = v[500] = 3.0  # crawling: a non-advancing sample here is real, not a glitch
    clean = clean_lap(_with_columns(lap, distance_m=d, speed_kph=v))
    assert clean.repairs == 1 and clean.dropped_glitches == 0
    assert np.all(np.diff(clean.distance_m) > 0)
    cmp = compare_laps(syn.sample_lap(REF), _with_columns(lap, distance_m=d, speed_kph=v))
    assert cmp.quality.repairs == 1
    assert np.all(np.diff(cmp.target.time_s) > 0)


@pytest.mark.parametrize("drift", [0.031, -0.031, 0.05])
def test_length_mismatch_over_3pct_raises(drift):
    with pytest.raises(LapInvalidError, match="length"):
        compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, drift=drift))


def test_length_mismatch_under_3pct_is_accepted():
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, drift=0.029))
    assert abs(cmp.delta_s).max() < 3e-3


def test_duration_tolerance_is_0p15s():
    clean_lap(syn.sample_lap(REF, lap_time_s=T + 0.149))
    with pytest.raises(LapInvalidError, match="duration"):
        clean_lap(syn.sample_lap(REF, lap_time_s=T + 0.151))


def test_final_delta_mismatch_raises_insufficient_data():
    # Telemetry says the laps are equal, official times say 0.1 s apart: refuse to answer.
    with pytest.raises(InsufficientDataError, match="0.1"):
        compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, hz=7, lap_time_s=T + 0.1))


def test_errors_are_not_silent_for_missing_telemetry():
    lap = syn.sample_lap(REF)
    empty = LapData(TelemetryFrame.empty(), T)
    with pytest.raises(LapInvalidError):
        compare_laps(lap, empty)
    assert set(TELEMETRY_COLUMNS) == set(lap.telemetry.columns())


# ---------------------------------------------------------------- sector cross-check
def test_sector_check_agrees_on_synthetic_laps():
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9)
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(slow, hz=8, drift=0.01))
    diag = sector_check(cmp)
    assert diag.available and diag.ok
    assert [c.sector for c in diag.checks] == [1, 2, 3]
    assert diag.checks[0].boundary_m == pytest.approx(REF.length_m / 3, abs=2.0)
    assert all(abs(c.discrepancy_s) < 0.005 for c in diag.checks)
    # hairpin (~2400 m) sits in sector 2, so S1 is equal and S2 carries the loss
    s1, s2, s3 = diag.sector_deltas_s
    assert abs(s1) <= 0.002 and s2 > 0.1 and abs(s3) <= 0.002


def test_sector_check_flags_disagreement():
    lap = syn.sample_lap(REF)
    s1, s2, s3 = lap.sector_s
    lying = LapData(lap.telemetry, lap.lap_time_s, sector_s=(s1 + 0.1, s2 - 0.1, s3))
    diag = sector_check(compare_laps(lap, lying))
    assert diag.available and not diag.ok
    assert not diag.checks[0].ok and diag.checks[0].discrepancy_s == pytest.approx(-0.1, abs=0.002)
    assert diag.checks[1].ok


def test_sector_check_unavailable_without_sector_times():
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, sectors=False))
    diag = sector_check(cmp)
    assert not diag.available and not diag.ok and diag.checks == ()
