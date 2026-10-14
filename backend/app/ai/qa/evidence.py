"""Phase 7E cause attribution: the fixed evidence structure an answer may only phrase.

    build_evidence(factsheet, segments=None) -> Evidence
      driver_factors  the findings' causes for the scoped segments, each with the metric that shows it
                      (only causes whose metric went the same way as the segment's result)
      tire_factors    real differences between the two laps' tyres: compound, age, new vs used set
      traffic         "available" | "unavailable" (conditions.traffic_data)
      conditions      non-green track status or rain on either lap
      data_quality    high | medium | low (segment confidences, the sector cross-check)

Policy (docs/qa_behavior.md, locked by tests/test_qa_policy.py): an answer says "evidence
suggests" and names only these factors as contributing factors; with traffic unavailable it says
traffic cannot be assessed; it never names a single definitive cause unless exactly one factor
exists and data quality is high (`single_cause_allowed`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from ..fallback import CAUSE_METRIC, FRAGMENTS, POSITIVE_HELPS
from ..schemas import FactSheet

MAX_SEGMENTS = 3
TRAFFIC_FACT = "conditions.traffic_data"


@dataclass(frozen=True)
class Factor:
    kind: str                       # a findings cause, or tyre_age / compound / fresh_tyre / track_status / rainfall
    segment: str | None             # fact prefix (driver factors), None otherwise
    label: str
    fact_ids: tuple[str, ...]
    description: str                # template wording; every number a display string of fact_ids


@dataclass
class Evidence:
    scope: list[str]
    driver_factors: list[Factor]
    tire_factors: list[Factor]
    traffic: Literal["available", "unavailable"]
    conditions: list[Factor]
    data_quality: Literal["high", "medium", "low"]
    notes: list[str] = field(default_factory=list)

    @property
    def factors(self) -> list[Factor]:
        return [*self.driver_factors, *self.tire_factors, *self.conditions]

    @property
    def single_cause_allowed(self) -> bool:
        return len(self.factors) == 1 and self.data_quality == "high"

    def fact_ids(self) -> list[str]:
        ids = [i for f in self.factors for i in f.fact_ids]
        ids += [f"{p}.{x}" for p in self.scope for x in ("time_loss_s", "label", "causes", "confidence")]
        return list(dict.fromkeys([*ids, TRAFFIC_FACT]))

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["single_cause_allowed"] = self.single_cause_allowed
        return out


def build_evidence(fs: FactSheet, segments: list[str] | None) -> Evidence:
    facts = {f.id: f for f in fs.facts}
    cats = {c.id: c.value for c in fs.categorical_facts}
    target, reference = fs.drivers.target.code, fs.drivers.reference.code

    def value(fid: str) -> Any:
        f = facts.get(fid)
        return None if f is None else f.value

    def shown(fid: str) -> str:
        return facts[fid].display[0]

    scope = [p for p in (segments or fs.rankings.largest_losses) if f"{p}.time_loss_s" in facts][:MAX_SEGMENTS]
    driver: list[Factor] = []
    for prefix in scope:
        loss = value(f"{prefix}.time_loss_s")
        raw = cats.get(f"{prefix}.causes")
        if loss is None or loss == 0 or not isinstance(raw, list):
            continue
        gained = loss < 0
        label = str(cats.get(f"{prefix}.label") or prefix)
        seen: set[str] = set()
        for cause in raw:
            metric = CAUSE_METRIC.get(cause)
            if metric is None or metric in seen:
                continue
            fid = f"{prefix}.{metric}"
            v = value(fid)
            if v is None or v == 0 or ((v > 0) == POSITIVE_HELPS[metric]) != gained:
                continue                    # only metrics in line with the result explain it
            seen.add(metric)
            driver.append(Factor(cause, prefix, label, (f"{prefix}.time_loss_s", fid, f"{prefix}.causes"),
                                 f"{label}: {target} {FRAGMENTS[metric](shown(fid), v > 0)}"))

    tires: list[Factor] = []
    tc, rc = cats.get("target.compound"), cats.get("reference.compound")
    if tc and rc and tc != rc:
        tires.append(Factor("compound", None, "compound", ("target.compound", "reference.compound"),
                            f"{target} was on the {str(tc).lower()} tyre, {reference} on the {str(rc).lower()}"))
    ta, ra = value("target.tyre_age_laps"), value("reference.tyre_age_laps")
    if ta is not None and ra is not None and ta != ra:
        tires.append(Factor("tyre_age", None, "tyre age", ("target.tyre_age_laps", "reference.tyre_age_laps"),
                            f"{target}'s tyres were {shown('target.tyre_age_laps')} laps old, "
                            f"{reference}'s {shown('reference.tyre_age_laps')} laps"))
    tf, rf = cats.get("target.fresh_tyre"), cats.get("reference.fresh_tyre")
    if isinstance(tf, bool) and isinstance(rf, bool) and tf != rf:
        fresh, used = (target, reference) if tf else (reference, target)
        tires.append(Factor("fresh_tyre", None, "new set", ("target.fresh_tyre", "reference.fresh_tyre"),
                            f"{fresh}'s set was new when fitted, {used}'s was used"))

    conditions: list[Factor] = []
    for side, code in (("target", target), ("reference", reference)):
        flags = cats.get(f"{side}.track_status_flags")
        if isinstance(flags, list) and any(f != "green" for f in flags):
            shown_flags = ", ".join(f for f in flags if f != "green").replace("_", " ")
            conditions.append(Factor("track_status", None, "track status", (f"{side}.track_status_flags",),
                                     f"{code}'s lap had track status: {shown_flags}"))
        if cats.get(f"{side}.rainfall") is True:
            conditions.append(Factor("rainfall", None, "rain", (f"{side}.rainfall",), f"rain was reported on {code}'s lap"))

    traffic_raw = cats.get(TRAFFIC_FACT, fs.conditions.traffic_data)
    traffic: Literal["available", "unavailable"] = "available" if traffic_raw == "available" else "unavailable"
    confidences = [str(cats.get(f"{p}.confidence") or "low") for p in scope]
    if "low" in confidences or cats.get("quality.sector_check_ok") is False:
        quality: Literal["high", "medium", "low"] = "low"
    elif confidences and all(c == "high" for c in confidences):
        quality = "high"
    else:
        quality = "medium"
    notes = ["causes are rules whose thresholds were met (contributing factors), not proof of cause"]
    return Evidence(scope, driver, tires, traffic, conditions, quality, notes)
