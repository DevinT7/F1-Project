"""Phase 7A schemas: the FactSheet the LLM receives and the Report it must return.

The FactSheet is the ONLY input of the AI layer. Every number a report may contain is a Fact
(id, value, unit, the exact display strings allowed, and a meaning that states the sign
convention in words); every non-numeric value it may name is a CategoricalFact. Nulls are facts
too ("unavailable — …"), so missing data can be stated as missing. See docs/factsheet_spec.md.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FACTSHEET_VERSION = "1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Fact(_Model):
    id: str = Field(description='"{prefix}.{field}", e.g. "T7.time_loss_s"')
    value: float | int | None
    unit: str
    display: list[str] = Field(description="the exact digit strings a report may use, primary precision first; [] when null")
    meaning: str = Field(description="what the number means, sign convention in words; 'unavailable — …' when null")
    source: str = Field(description='JSON pointer into the source ("findings:/corners/5/time_loss_s", "tire:/lap") '
                                    'or "derived:…" when there is no source value (e.g. tyre unavailable)')


class CategoricalFact(_Model):
    id: str
    value: str | bool | list[str] | None
    allowed: list[str] | None = Field(default=None, description="the closed set of values this fact can take, when there is one")
    meaning: str
    source: str


class SessionSection(_Model):
    key: str | None
    event: str | None
    session_type: str | None = Field(description="FastF1 session code: Q, R, FP1, …")
    session_name: str | None
    date: str | None


class DriverSection(_Model):
    code: str
    team: str | None
    lap: int
    lap_time_s: float


class DriversSection(_Model):
    target: DriverSection
    reference: DriverSection


class LapSummarySection(_Model):
    total_delta_s: float | None
    sector_deltas_s: list[float | None]
    top_speed_diff_kph: float | None


class SegmentSection(_Model):
    label: str
    fact_prefix: str
    kind: Literal["corner", "straight", "sector"]
    time_loss_s: float | None
    confidence: str
    causes: list[str]
    rank: int | None


class RankingsSection(_Model):
    largest_losses: list[str] = Field(description="fact prefixes, biggest loss first")
    largest_gains: list[str]


class ConditionsSection(_Model):
    track_temp_c: float | None
    air_temp_c: float | None
    track_status_flags: list[str]
    traffic_data: Literal["unavailable"] = "unavailable"


class DataQualitySection(_Model):
    low_confidence_segments: list[str]
    notes: list[str]


class FactSheet(_Model):
    factsheet_version: str = FACTSHEET_VERSION
    session: SessionSection
    drivers: DriversSection
    lap_summary: LapSummarySection
    segments: list[SegmentSection]
    rankings: RankingsSection
    tire: dict[str, Any] = Field(description='tyre contract values keyed without "tire.", plus status; '
                                             'or {"status": "unavailable", "reason": "…"}')
    conditions: ConditionsSection
    data_quality: DataQualitySection
    facts: list[Fact]
    categorical_facts: list[CategoricalFact]
    factsheet_hash: str = Field(default="", description="SHA-256 of the canonical JSON of everything else")

    def fact(self, fact_id: str) -> Fact:
        return next(f for f in self.facts if f.id == fact_id)


# ---------------------------------------------------------------- the report the LLM returns
class Bullet(_Model):
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(description="every fact the text relies on; validated in 7B")


class Report(_Model):
    headline: Bullet
    biggest_time_loss: list[Bullet]
    strongest_areas: list[Bullet]
    tire_notes: list[Bullet]
    driving_suggestions: list[Bullet]
    strategy_options: list[Bullet]
    data_limitations: list[Bullet]
    generated_by: Literal["llm", "template"]
    prompt_version: str
    factsheet_hash: str
