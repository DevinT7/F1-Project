"""Phase 2 — integration on the real fixture (2024 R5 Qualifying, FastF1 3.5.3).

Every pair of valid laps in the fixture must reproduce its official lap-time gap
within 0.02 s. The sector cross-check runs too; its result is recorded, not asserted
(see docs/progress.md: integrated-distance drift leaves ~40-60 ms mid-lap uncertainty).
"""
from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pytest

from app.processing.delta import compare_laps, sector_check
from app.processing.models import LapData
from app.storage.repository import ParquetRepository

STORE = ParquetRepository(Path(__file__).parent / "fixtures" / "ingestion" / "store")
KEY = "2024_05_Q"
VALID = [(l.driver, l.lap_number) for l in STORE.list_laps(KEY) if l.is_valid_for_comparison]
PAIRS = list(itertools.permutations(VALID, 2))


def _lap(key: tuple[str, int]) -> LapData:
    return LapData.from_repository(STORE, KEY, *key)


def test_fixture_has_a_valid_lap_per_driver():
    assert {driver for driver, _ in VALID} == {"VER", "PER", "ALO"}


@pytest.mark.parametrize("ref, tgt", PAIRS, ids=[f"{a[0]}{a[1]}-vs-{b[0]}{b[1]}" for a, b in PAIRS])
def test_final_delta_matches_official_gap(ref, tgt):
    a, b = _lap(ref), _lap(tgt)
    cmp = compare_laps(a, b)
    assert abs(cmp.delta_s[-1] - (b.lap_time_s - a.lap_time_s)) <= 0.02
    assert abs(cmp.quality.final_delta_error_s) <= 0.02
    assert abs(cmp.quality.length_scale_factor - 1) < 0.03
    assert np.all(np.diff(cmp.target.time_s) > 0)
    diag = sector_check(cmp)
    assert diag.available and len(diag.checks) == 3
    assert diag.checks[2].ok  # the finish line is the integrity check above


def test_real_pair_is_antisymmetric():
    a, b = _lap(VALID[0]), _lap(VALID[1])
    ab, ba = compare_laps(a, b), compare_laps(b, a)
    # grids differ (each run uses its own reference length), so compare on a shared axis
    shared = np.linspace(0, min(ab.grid_m[-1], ba.grid_m[-1]), 500)
    ab_on = np.interp(shared / ab.grid_m[-1], ab.grid_m / ab.grid_m[-1], ab.delta_s)
    ba_on = np.interp(shared / ba.grid_m[-1], ba.grid_m / ba.grid_m[-1], ba.delta_s)
    assert np.abs(ab_on + ba_on).max() < 0.01


def test_fastest_lap_comparison_is_plausible():
    ver, alo = _lap(next(k for k in VALID if k[0] == "VER")), _lap(next(k for k in VALID if k[0] == "ALO"))
    cmp = compare_laps(ver, alo)
    assert cmp.delta_s[-1] == pytest.approx(94.148 - 93.66, abs=0.001)  # official Q3 gap
    assert 5300 < cmp.grid_m[-1] < 5500                                  # Shanghai ~5.45 km
    assert np.abs(cmp.speed_diff_kph).max() < 80
    assert set(np.unique(cmp.target.gear)) <= set(range(0, 9))
