"""Phase 6A exclusion rules: one pure function per rule.

Each rule takes the laps still kept (a DataFrame) and the config, and returns a RuleResult:
a boolean mask (True = drop) aligned to the input and its reason code (the function name).
apply_rules() runs them in the fixed order of RULES; a row is attributed to the FIRST rule that
drops it, so per-rule counts plus kept rows always equal the input (conservation).

Expected columns: session_key, driver, lap_number, stint, compound, lap_time_s, pit_in_time_s,
pit_out_time_s, track_status, is_accurate, deleted, rainfall.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .config import (
    GREEN_ONLY, MAD_TO_SIGMA, RED_FLAG_CODE, UNKNOWN_COMPOUNDS, WET_COMPOUNDS, ExclusionConfig,
)


@dataclass(frozen=True)
class RuleResult:
    mask: "pd.Series[bool]"
    reason: str


Rule = Callable[[pd.DataFrame, ExclusionConfig], RuleResult]


def _result(mask: "pd.Series[bool]", reason: str) -> RuleResult:
    return RuleResult(mask=mask.fillna(False).astype(bool), reason=reason)


def normalize_compound(values: "pd.Series[Any]") -> "pd.Series[str]":
    """Upper-case, stripped; missing → "" (so it counts as unknown)."""
    return values.fillna("").astype(str).str.strip().str.upper()


def _status_set(status: object) -> frozenset[str]:
    return frozenset(status) if isinstance(status, str) else frozenset()


# ---------------------------------------------------------------- the eight rules, in order
def lap_1_or_restart(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    """Lap 1, and the first lap after a red-flag stoppage (per driver: the previous lap
    carried the red-flag code and this one does not). Both are standing-start laps."""
    red = df["track_status"].map(lambda s: RED_FLAG_CODE in _status_set(s))
    red_laps = set(zip(df.loc[red, "session_key"], df.loc[red, "driver"], df.loc[red, "lap_number"]))
    prev_red = pd.Series(
        [(s, d, n - 1) in red_laps for s, d, n in zip(df["session_key"], df["driver"], df["lap_number"])],
        index=df.index,
    )
    return _result((df["lap_number"] == 1) | (prev_red & ~red), "lap_1_or_restart")


def in_or_out_lap(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    return _result(df["pit_in_time_s"].notna() | df["pit_out_time_s"].notna(), "in_or_out_lap")


def non_green_track_status(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    """Keep only laps whose status SET is exactly {"1"} ("11" is green; "12" is not)."""
    return _result(df["track_status"].map(lambda s: _status_set(s) != GREEN_ONLY), "non_green_track_status")


def inaccurate_or_deleted(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    """IsAccurate false, Deleted true, or no lap time. Deleted=None means "unknown", not deleted."""
    deleted = df["deleted"].map(lambda v: v is True or v == True)  # noqa: E712 (numpy bools too)
    return _result((df["is_accurate"] != True) | deleted | df["lap_time_s"].isna(), "inaccurate_or_deleted")  # noqa: E712


def wet_conditions(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    rain = df["rainfall"].map(lambda v: v is True or v == True)  # noqa: E712
    return _result(rain | normalize_compound(df["compound"]).isin(WET_COMPOUNDS), "wet_conditions")


def unknown_compound(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    return _result(normalize_compound(df["compound"]).isin(UNKNOWN_COMPOUNDS), "unknown_compound")


def _stint_keys(df: pd.DataFrame) -> list["pd.Series[Any]"]:
    return [df["session_key"], df["driver"], df["stint"].fillna(-1)]


def stint_outlier(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    """Within each (session, driver, stint) with ≥ outlier_min_laps laps, drop laps slower than
    median + k × 1.4826 × MAD. Stints whose MAD is 0 are left alone (no spread to judge by)."""
    mask = pd.Series(False, index=df.index)
    for _, group in df.groupby(_stint_keys(df), sort=False):
        times = group["lap_time_s"].astype(float)
        if len(times) < cfg.outlier_min_laps:
            continue
        median = float(np.median(times))
        mad = float(np.median(np.abs(times - median)))
        if mad <= 0:
            continue
        mask.loc[group.index] = times > median + cfg.outlier_mad_k * MAD_TO_SIGMA * mad
    return _result(mask, "stint_outlier")


def short_stint(df: pd.DataFrame, cfg: ExclusionConfig) -> RuleResult:
    """Stints with fewer than min_clean_stint_laps remaining clean laps (unless keep_short_stints)."""
    if cfg.keep_short_stints:
        return _result(pd.Series(False, index=df.index), "short_stint")
    sizes = df.groupby(_stint_keys(df), sort=False)["lap_number"].transform("size")
    return _result(sizes < cfg.min_clean_stint_laps, "short_stint")


RULES: tuple[Rule, ...] = (
    lap_1_or_restart,
    in_or_out_lap,
    non_green_track_status,
    inaccurate_or_deleted,
    wet_conditions,
    unknown_compound,
    stint_outlier,
    short_stint,
)
RULE_NAMES: tuple[str, ...] = tuple(rule.__name__ for rule in RULES)


@dataclass(frozen=True)
class ExclusionResult:
    kept: pd.DataFrame
    reasons: "pd.Series[Any]"      # per input row: reason code of the first dropping rule, or None (kept)
    counts: dict[str, int]         # rule → rows it dropped, every rule present


def apply_rules(df: pd.DataFrame, cfg: ExclusionConfig) -> ExclusionResult:
    reasons = pd.Series([None] * len(df), index=df.index, dtype=object)
    counts = {name: 0 for name in RULE_NAMES}
    kept = df
    for rule in RULES:
        if kept.empty:
            break
        result = rule(kept, cfg)
        drop_index = kept.index[result.mask.to_numpy()]
        reasons.loc[drop_index] = result.reason
        counts[result.reason] = len(drop_index)
        kept = kept.loc[~result.mask.to_numpy()]
    return ExclusionResult(kept=kept, reasons=reasons, counts=counts)
