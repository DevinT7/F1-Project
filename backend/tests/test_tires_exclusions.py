"""Phase 6A — one test per exclusion rule (written before the builder), plus order and conservation.

Each rule test uses a tiny synthetic lap table where exactly the rows the rule targets are dropped.
Rules see only the rows still kept after the earlier rules (that is what makes order matter).
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.tires import exclusions as X
from app.tires.config import ExclusionConfig

CFG = ExclusionConfig()


def laps(rows: list[dict]) -> pd.DataFrame:
    """Clean, green, dry, accurate laps unless a row overrides a field."""
    base = {
        "session_key": "2024_05_R", "driver": "VER", "lap_number": 2, "stint": 1, "compound": "MEDIUM",
        "lap_time_s": 98.0, "pit_in_time_s": None, "pit_out_time_s": None, "track_status": "1",
        "is_accurate": True, "deleted": False, "rainfall": False,
    }
    return pd.DataFrame([{**base, **r} for r in rows])


def stint(times: list[float], **extra) -> list[dict]:
    return [{"lap_number": 2 + i, "lap_time_s": t, **extra} for i, t in enumerate(times)]


def dropped(rule, df: pd.DataFrame, cfg: ExclusionConfig = CFG) -> list[int]:
    result = rule(df, cfg)
    assert result.reason == rule.__name__
    assert result.mask.dtype == bool and len(result.mask) == len(df)
    return df.index[result.mask].tolist()


# ---------------------------------------------------------------- one test per rule
def test_lap_1_or_restart():
    df = laps([
        {"lap_number": 1}, {"lap_number": 2},
        {"lap_number": 30, "track_status": "15"}, {"lap_number": 31},   # 31 = first lap after the red flag
        {"lap_number": 32},
        {"driver": "NOR", "lap_number": 1}, {"driver": "NOR", "lap_number": 31},  # NOR had no red-flag lap
    ])
    assert dropped(X.lap_1_or_restart, df) == [0, 3, 5]


def test_in_or_out_lap():
    df = laps([{}, {"pit_in_time_s": 1200.5}, {"pit_out_time_s": 1230.1}, {"pit_in_time_s": None}])
    assert dropped(X.in_or_out_lap, df) == [1, 2]


@pytest.mark.parametrize("status, drop", [
    ("1", False), ("11", False), ("2", True), ("12", True), ("21", True), ("4", True),
    ("1267", True), ("6", True), ("7", True), (None, True), ("", True),
])
def test_non_green_track_status_treats_status_as_a_set(status, drop):
    assert dropped(X.non_green_track_status, laps([{"track_status": status}])) == ([0] if drop else [])


def test_inaccurate_or_deleted():
    df = laps([{}, {"is_accurate": False}, {"deleted": True}, {"lap_time_s": None}, {"deleted": None}])
    assert dropped(X.inaccurate_or_deleted, df) == [1, 2, 3]  # deleted=None (unknown) is not a deletion


def test_wet_conditions():
    df = laps([{}, {"rainfall": True}, {"compound": "INTERMEDIATE"}, {"compound": "WET"}, {"rainfall": None}])
    assert dropped(X.wet_conditions, df) == [1, 2, 3]


def test_unknown_compound():
    df = laps([{}, {"compound": None}, {"compound": "UNKNOWN"}, {"compound": "TEST_UNKNOWN"}, {"compound": "HARD"}])
    assert dropped(X.unknown_compound, df) == [1, 2, 3]


def test_stint_outlier():
    times = [98.0, 98.2, 98.1, 98.3, 98.2, 104.0, 98.4]  # one lap 6 s off the pace (e.g. a moment)
    df = laps(stint(times) + stint([97.0, 99.0, 110.0, 97.5], driver="NOR"))  # NOR: < 5 laps, not tested
    assert dropped(X.stint_outlier, df) == [5]


def test_stint_outlier_skips_zero_spread_stints():
    df = laps(stint([98.0] * 6))
    assert dropped(X.stint_outlier, df) == []


def test_short_stint():
    df = laps(stint([98.0] * 5) + stint([97.0] * 4, stint=2))
    assert dropped(X.short_stint, df) == [5, 6, 7, 8]
    assert dropped(X.short_stint, df, ExclusionConfig(keep_short_stints=True)) == []


def test_rules_normalize_compound_case():
    df = laps([{"compound": "intermediate"}, {"compound": " unknown "}])
    assert dropped(X.wet_conditions, df) == [0]
    assert dropped(X.unknown_compound, df) == [1]


# ---------------------------------------------------------------- pipeline
def test_rules_run_in_the_documented_order():
    assert [r.__name__ for r in X.RULES] == [
        "lap_1_or_restart", "in_or_out_lap", "non_green_track_status", "inaccurate_or_deleted",
        "wet_conditions", "unknown_compound", "stint_outlier", "short_stint",
    ]


def test_each_row_is_attributed_to_the_first_rule_that_drops_it():
    df = laps([{"lap_number": 1, "pit_out_time_s": 10.0, "track_status": "4"}] + stint([98.0] * 5, stint=2))
    result = X.apply_rules(df, CFG)
    assert result.reasons.iloc[0] == "lap_1_or_restart"  # also an out lap and under SC: counted once
    assert result.counts["lap_1_or_restart"] == 1 and result.counts["in_or_out_lap"] == 0


def test_order_matters_for_short_stints():
    # 5 laps but one is a pit-in lap → only 4 clean laps remain → the whole stint goes
    df = laps(stint([98.0] * 4) + [{"lap_number": 6, "lap_time_s": 120.0, "pit_in_time_s": 5.0}])
    result = X.apply_rules(df, CFG)
    assert result.counts == {**{r.__name__: 0 for r in X.RULES}, "in_or_out_lap": 1, "short_stint": 4}
    assert result.kept.empty


def test_conservation_dropped_plus_kept_equals_input():
    rows = (
        [{"lap_number": 1}]
        + stint([98.0, 98.1, 98.3, 98.2, 103.0, 98.4, 98.5])
        + stint([97.0, 97.2], stint=2)
        + [{"lap_number": 40, "track_status": "26"}, {"lap_number": 41, "compound": "WET"},
           {"lap_number": 42, "is_accurate": False}, {"lap_number": 43, "compound": None}]
    )
    df = laps(rows)
    result = X.apply_rules(df, CFG)
    assert len(result.kept) + sum(result.counts.values()) == len(df)
    assert result.reasons.isna().sum() == len(result.kept)
    assert set(result.kept.index).isdisjoint(set(result.reasons.dropna().index))
