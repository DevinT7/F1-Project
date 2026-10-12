"""Phase 3 — corner segmentation, per-corner metrics, attribution and findings.

Known answers come from tests/fixtures/synthetic.py: a target that brakes exactly 18 m
earlier into the hairpin, and one whose T2 minimum speed is exactly 7 km/h lower.
Braking points need a fine sample rate (100 Hz here): at FastF1's ~8 Hz the boolean
brake channel is only resolved to ~10 m at top speed.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.processing import corner_metrics as M
from app.processing.config import Thresholds
from app.processing.delta import compare_laps
from app.processing.insights import Finding, FindingsReport, generate_findings, rank_corner_findings
from app.processing.models import AlignedLap
from app.processing.segments import corner_type, detect_apexes, partition
from tests.fixtures import synthetic as syn

REF = syn.make_profile()
CORNERS = syn.official_corners(REF)
EARLY_BRAKE = syn.make_profile(knot_shifts=tuple((k, -18.0) for k in syn.HAIRPIN_BRAKE_KNOTS))
LOW_MIN = syn.make_profile(speed_offset=(syn.T2_APEX_M, 40.0, -7.0))


def _report(target_profile, *, hz=100.0, ref_hz=100.0, **kwargs) -> FindingsReport:
    cmp = compare_laps(syn.sample_lap(REF, hz=ref_hz, driver="REF"),
                       syn.sample_lap(target_profile, hz=hz, driver="TGT", seed=1))
    return generate_findings(cmp, CORNERS, **kwargs)


def _corner(report: FindingsReport, label: str) -> Finding:
    return next(f for f in report.corners if f.label == label)


# ---------------------------------------------------------------- known answers
def test_braking_18m_earlier():
    report = _report(EARLY_BRAKE)
    hairpin = _corner(report, "Turn 3")
    assert hairpin.metrics["braking_start_diff_m"] == pytest.approx(-18.0, abs=1.0)
    assert "early_braking_loss" in hairpin.causes
    assert hairpin.time_loss_s > 0.02
    # the other braked corners are untouched
    for label in ("Turn 1", "Turn 2"):
        assert abs(_corner(report, label).metrics["braking_start_diff_m"]) < 1.0
        assert _corner(report, label).causes == []


def test_min_speed_7kph_lower():
    report = _report(LOW_MIN)
    t2 = _corner(report, "Turn 2")
    assert t2.metrics["min_speed_diff_kph"] == pytest.approx(-7.0, abs=0.5)
    assert "low_min_speed" in t2.causes
    assert t2.time_loss_s > 0.02
    assert report.largest_losses[0] == t2.id


def test_identical_laps_have_no_causes_and_low_confidence():
    report = _report(REF, hz=10.0, ref_hz=10.0)
    for finding in report.corners:
        assert finding.causes == []
        assert finding.confidence == "low"           # below the 0.02 s noise floor
        assert abs(finding.time_loss_s) < 1e-3
    assert report.largest_losses == [] and report.largest_gains == []


# ---------------------------------------------------------------- segments
def test_segments_tile_the_lap_and_losses_sum_to_total():
    report = _report(syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9), hz=10.0, ref_hz=10.0)
    spans = [(f.segment.start_m, f.segment.end_m) for f in report.corners]
    assert spans[0][0] == 0.0 and spans[-1][1] == pytest.approx(report.lap_summary.segment.end_m)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    total = sum(f.time_loss_s for f in report.corners)
    assert total == pytest.approx(report.lap_summary.time_loss_s, abs=1e-9)
    for f in report.corners:
        assert f.entry_loss_s + f.exit_loss_s == pytest.approx(f.time_loss_s, abs=1e-12)
    assert report.quality.tiling_error_s <= 1e-9


def test_apex_sources_and_fallbacks():
    report = _report(REF, hz=10.0, ref_hz=10.0)
    by_label = {f.label: f for f in report.corners}
    assert by_label["Turn 1"].apex_source == "matched"
    assert by_label["Turn 1"].segment.apex_m == pytest.approx(900.0, abs=3.0)
    assert by_label["Turn 5"].apex_source == "official"          # decoy on the straight
    detected = [f for f in report.corners if f.apex_source == "detected"]
    assert len(detected) == 1 and detected[0].segment.apex_m == pytest.approx(4148.0, abs=3.0)
    assert detected[0].corner_number is None
    assert [f.corner_type for f in report.corners if f.corner_number in (1, 3)] == ["slow", "slow"]
    assert by_label["Turn 2"].corner_type == "medium" and by_label["Turn 4"].corner_type == "medium"  # ~203 km/h


def test_flat_out_corner_has_no_braking_point():
    report = _report(REF, hz=10.0, ref_hz=10.0)
    t4 = _corner(report, "Turn 4")  # 200 km/h sweeper, no brake
    assert t4.reference["braking_start_m"] is None and t4.target["braking_start_m"] is None
    assert t4.metrics["braking_start_diff_m"] is None
    assert t4.reference["entry_speed_kph"] is not None  # falls back to A - 100 m


def test_xy_check_flags_misplaced_official_corner():
    moved = [c.model_copy(update={"x": c.x + 2000.0}) if c.number == 1 else c for c in CORNERS]
    cmp = compare_laps(syn.sample_lap(REF), syn.sample_lap(REF, hz=8))
    t1 = next(f for f in generate_findings(cmp, moved).corners if f.label == "Turn 1")
    assert any("X/Y" in note for note in t1.notes)
    assert t1.confidence != "high"


def test_detect_apexes_prominence():
    grid = np.arange(0.0, 1000.0, 1.0)
    speed = 250 - 100 * np.exp(-((grid - 300) / 30) ** 2) - 5 * np.exp(-((grid - 700) / 30) ** 2)
    apexes = detect_apexes(grid, speed, Thresholds())
    assert apexes.tolist() == [300.0]  # the 5 km/h dip is below the 8 km/h prominence


@pytest.mark.parametrize("speed, kind", [(119.9, "slow"), (120.0, "medium"), (220.0, "medium"), (220.1, "fast"), (None, None)])
def test_corner_type_boundaries(speed, kind):
    assert corner_type(speed, Thresholds()) == kind


def test_partition_midpoints():
    assert partition([100.0, 300.0, 700.0], 1000.0) == [(0.0, 200.0), (200.0, 500.0), (500.0, 1000.0)]
    assert partition([], 1000.0) == [(0.0, 1000.0)]


# ---------------------------------------------------------------- antisymmetry / determinism
def test_antisymmetry_with_shared_segments():
    a = syn.sample_lap(REF, hz=100, driver="A")
    b = syn.sample_lap(EARLY_BRAKE, hz=100, driver="B", seed=2)
    ab = generate_findings(compare_laps(a, b), CORNERS)
    ba = generate_findings(compare_laps(b, a), CORNERS, segments=ab.segments)
    for f_ab, f_ba in zip(ab.corners, ba.corners):
        assert f_ab.label == f_ba.label
        assert f_ab.time_loss_s == pytest.approx(-f_ba.time_loss_s, abs=1e-9)
        for key, value in f_ab.metrics.items():
            if value is None:
                assert f_ba.metrics[key] is None
            else:
                assert value == pytest.approx(-f_ba.metrics[key], abs=1e-9), key
    hairpin_ab, hairpin_ba = _corner(ab, "Turn 3"), _corner(ba, "Turn 3")
    assert "early_braking_loss" in hairpin_ab.causes and "late_braking_gain" in hairpin_ba.causes


def test_findings_are_deterministic():
    one = _report(EARLY_BRAKE).model_dump_json()
    two = _report(EARLY_BRAKE).model_dump_json()
    assert one == two


def test_ranking_ties_broken_by_distance():
    def finding(label, start, loss):
        return Finding(id=label, kind="corner", label=label, segment={"start_m": start, "end_m": start + 10, "apex_m": start + 5},
                       time_loss_s=loss, confidence="high")
    ranked = rank_corner_findings([finding("C", 300.0, 0.1), finding("A", 100.0, -0.1), finding("B", 200.0, 0.1),
                                   finding("D", 50.0, 0.3)])
    assert [f.label for f in ranked] == ["D", "A", "B", "C"]
    assert [f.rank for f in ranked] == [1, 2, 3, 4]


# ---------------------------------------------------------------- thresholds / confidence / JSON
def test_custom_thresholds_change_attribution():
    report = _report(LOW_MIN, thresholds=Thresholds(min_speed_diff_kph=10.0))
    t2 = _corner(report, "Turn 2")
    assert "low_min_speed" not in t2.causes
    assert t2.causes == ["unclear"] or set(t2.causes) <= {"lower_exit_speed", "late_throttle", "early_braking_loss"}


def test_unclear_when_loss_without_triggered_rule():
    # a uniform 0.3 % slowdown loses time everywhere but trips no per-corner rule
    report = _report(syn.make_profile(speed_scale=0.997), hz=10.0, ref_hz=10.0)
    lossy = [f for f in report.corners if f.time_loss_s >= 0.02]
    assert lossy and all(f.causes == ["unclear"] for f in lossy)


def test_json_has_no_nan_and_nulls_not_zeros():
    report = _report(EARLY_BRAKE)
    text = json.dumps(report.model_dump(mode="json"), allow_nan=False)
    data = json.loads(text)
    t4 = next(f for f in data["corners"] if f["label"] == "Turn 4")
    assert t4["metrics"]["braking_start_diff_m"] is None
    assert data["lap_summary"]["kind"] == "lap_summary"
    assert data["lap_summary"]["metrics"]["corner_count"] == len(data["corners"])


def test_straights_and_sectors_are_reported():
    report = _report(syn.make_profile(speed_scale=0.99), hz=10.0, ref_hz=10.0)
    assert report.straights, "main/back straights are >= 300 m at full throttle"
    main = report.straights[0]
    assert main.kind == "straight" and main.segment.end_m - main.segment.start_m >= 300
    assert main.metrics["top_speed_diff_kph"] == pytest.approx(-0.01 * main.reference["top_speed_kph"], abs=0.5)
    assert "straight_line_deficit" in main.causes
    assert [f.label for f in report.sectors] == ["Sector 1", "Sector 2", "Sector 3"]
    assert sum(f.time_loss_s for f in report.sectors) == pytest.approx(report.lap_summary.time_loss_s, abs=0.005)


# ---------------------------------------------------------------- individual metric functions
def _lap(grid, speed, *, brake=None, throttle=None, gear=None) -> AlignedLap:
    n = len(grid)
    v_ms = np.maximum(np.asarray(speed, dtype=float), 1.0) / 3.6
    time = np.concatenate(([0.0], np.cumsum(np.diff(grid) / v_ms[1:])))
    return AlignedLap(
        driver="X", lap_number=1, lap_time_s=float(time[-1]), sector_s=(None, None, None),
        time_s=time, speed_kph=np.asarray(speed, dtype=float),
        throttle_pct=np.asarray(throttle if throttle is not None else np.full(n, 100.0), dtype=float),
        rpm=np.zeros(n), x=np.zeros(n), y=np.zeros(n),
        gear=np.asarray(gear if gear is not None else np.full(n, 5), dtype=np.int64),
        brake=np.asarray(brake if brake is not None else np.zeros(n, bool), dtype=bool),
        drs_open=np.zeros(n, bool), sample_count=n, length_scale_factor=1.0,
    )


def test_braking_start_requires_sustained_brake():
    grid = np.arange(0.0, 600.0, 1.0)
    brake = np.zeros(600, bool)
    brake[100:103] = True          # a 3 m blip (~0.04 s at 250 km/h): ignored
    brake[200:320] = True          # sustained
    lap = _lap(grid, np.full(600, 250.0), brake=brake)
    assert M.braking_start_m(grid, lap, 0.0, 500.0, 0.3) == 200.0
    assert M.braking_start_m(grid, lap, 330.0, 500.0, 0.3) is None


def test_min_entry_exit_and_throttle_metrics():
    grid = np.arange(0.0, 1000.0, 1.0)
    speed = 300 - 200 * np.exp(-((grid - 500) / 60) ** 2)
    throttle = np.where(grid < 400, 100.0, np.where(grid < 520, 0.0, np.where(grid < 560, 50.0, 100.0)))
    lap = _lap(grid, speed, throttle=throttle, gear=np.where(np.abs(grid - 500) < 50, 2, 6))
    assert M.min_speed(grid, lap, 500.0, 80.0) == (pytest.approx(100.0), 500.0)
    assert M.speed_at(grid, lap, 600.0) == pytest.approx(speed[600])
    assert M.throttle_reapply_m(grid, lap, 500.0, 900.0, 20.0) == 520.0
    assert M.full_throttle_m(grid, lap, 500.0, 900.0, 98.0, 0.3) == 560.0
    assert M.full_throttle_m(grid, lap, 500.0, 550.0, 98.0, 0.3) is None
    assert M.gear_stats(grid, lap, 400.0, 600.0, 500.0) == (2, 2)


def test_value_helpers_return_none_out_of_range():
    grid = np.arange(0.0, 100.0, 1.0)
    lap = _lap(grid, np.full(100, 200.0))
    assert M.speed_at(grid, lap, -5.0) is None and M.speed_at(grid, lap, 150.0) is None
    assert M.diff(None, 3.0) is None and M.diff(5.0, 3.0) == 2.0
