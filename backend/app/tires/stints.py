"""Per-driver stint summaries for one session (GET /api/v1/sessions/{key}/stints).

Clean-lap counts come from the same exclusion pipeline as the training dataset, so a stint is
"model_eligible" here exactly when the dataset would keep it (≥ min_clean_stint_laps clean laps).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ..ingestion.models import LapRecord
from .config import ExclusionConfig
from .dataset import laps_frame
from .exclusions import apply_rules


class StintSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stint: int
    compound: str | None = Field(description="normalized upper-case; the most common compound in the stint")
    first_lap: int
    last_lap: int
    tyre_age_at_start: int | None = Field(description="FastF1 TyreLife on the stint's first lap")
    fresh_tyre: bool | None = Field(description="new set at the start of the stint (False = used)")
    total_laps: int
    clean_laps: int = Field(description="laps surviving every Phase 6A exclusion rule")
    model_eligible: bool = Field(description="clean_laps ≥ the dataset's minimum clean stint length")


class DriverStints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver: str
    team: str | None = None
    stints: list[StintSummary]


def summarize_stints(laps: Sequence[LapRecord], cfg: ExclusionConfig = ExclusionConfig()) -> list[DriverStints]:
    df = laps_frame(laps)
    if df.empty:
        return []
    # Count laps that pass the LAP-level rules; the stint-level short_stint rule is what
    # model_eligible expresses, so it must not zero the count of a 4-clean-lap stint.
    kept = apply_rules(df, replace(cfg, keep_short_stints=True)).kept
    clean = Counter(zip(kept["driver"], kept["stint"]))
    out: list[DriverStints] = []
    for driver, dlaps in df[df["stint"].notna()].groupby("driver", sort=False):
        stints: list[StintSummary] = []
        for stint, s in dlaps.sort_values("lap_number").groupby("stint", sort=True):
            first = s.iloc[0]
            compounds = s["compound"].dropna()
            n_clean = clean.get((driver, stint), 0)
            stints.append(StintSummary(
                stint=int(cast(float, stint)),
                compound=str(compounds.mode().iloc[0]) if not compounds.empty else None,
                first_lap=int(s["lap_number"].min()),
                last_lap=int(s["lap_number"].max()),
                tyre_age_at_start=None if pd.isna(first["tyre_age"]) else int(first["tyre_age"]),
                fresh_tyre=None if pd.isna(first["fresh_tyre"]) else bool(first["fresh_tyre"]),
                total_laps=len(s),
                clean_laps=n_clean,
                model_eligible=n_clean >= cfg.min_clean_stint_laps,
            ))
        team = dlaps["team"].dropna()
        out.append(DriverStints(driver=str(driver), team=str(team.iloc[0]) if not team.empty else None, stints=stints))
    return out
