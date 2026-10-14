"""Phase 7E: per-intent FactSheet slices (pure functions of the FactSheet and the entities).

    build_slice(intent, factsheet, entities) -> Slice(fact_ids, categorical_ids, segments, evidence)

A slice is what the model sees for one question. Answers are validated against the FULL
FactSheet (any real fact may be cited), but must cite at least one fact of the slice; for
cause_attribution they may cite only the evidence's facts (docs/qa_behavior.md).

    where_lost_time       lap summary + top losses / gains
    why_faster_in_sector  the sector's facts + the segments inside it (by apex / midpoint) + causes
    tire_status           all tire.* facts + status / limits, the target's compound and tyre age
    focus_corners         top losses with causes, cause metrics and confidence
    when_to_pit           pit window, threshold, assumptions, confidence
    cause_attribution     the evidence structure (causes, tyre facts, track status, traffic availability)
    corner_detail         that segment's facts
    out_of_scope          nothing
"""
from __future__ import annotations

from dataclasses import dataclass

from ..fallback import CAUSE_METRIC
from ..schemas import FactSheet
from .evidence import TRAFFIC_FACT, Evidence, build_evidence
from .intents import Entities, Intent

TOP = 3
SEGMENT_CATS = ("label", "causes", "confidence", "notes")
DIFF_FIELDS = ("braking_start_diff_m", "entry_speed_diff_kph", "min_speed_diff_kph", "exit_speed_diff_kph",
               "time_to_full_throttle_diff_s", "throttle_reapply_diff_m", "braking_duration_diff_s", "gear_min_diff",
               "top_speed_diff_kph", "drs_open_fraction_diff", "time_diff_s")
LAP_FIELDS = ("time_loss_s", "total_delta_s", "official_gap_s", "sector_1_delta_s", "sector_2_delta_s",
              "sector_3_delta_s", "top_speed_diff_kph", "corner_count", "corners_lost", "corners_gained")
PIT_FIELDS = ("as_of_lap", "horizon_laps", "race_laps_total", "window_earliest_lap", "window_central_lap",
              "window_latest_lap", "threshold_s", "remaining_competitive_laps", "deg_s_per_lap", "clean_laps_used",
              "clean_laps_required")
PIT_CATS = ("status", "reason", "confidence", "confidence_reasons", "window_assumptions", "window_method",
            "window_reason", "window_pace_basis")
GEOMETRY = ("start_m", "end_m", "apex_m", "corner_number", "rank")


@dataclass
class Slice:
    intent: Intent
    fact_ids: list[str]
    categorical_ids: list[str]
    segments: list[str]
    evidence: Evidence | None = None

    @property
    def ids(self) -> set[str]:
        return {*self.fact_ids, *self.categorical_ids}


def _keep(fs: FactSheet, ids: list[str]) -> tuple[list[str], list[str]]:
    numeric = {f.id for f in fs.facts}
    cats = {c.id for c in fs.categorical_facts}
    ids = list(dict.fromkeys(ids))
    return [i for i in ids if i in numeric], [i for i in ids if i in cats]


def _segment_ids(prefix: str, *, metrics: str = "causes") -> list[str]:
    """A segment's loss, its cause metrics (or every diff), and its label / causes / confidence / notes."""
    fields = ["time_loss_s", "entry_loss_s", "exit_loss_s"]
    fields += DIFF_FIELDS if metrics == "all" else sorted(set(CAUSE_METRIC.values()))
    return [f"{prefix}.{x}" for x in fields] + [f"{prefix}.{x}" for x in SEGMENT_CATS]


def segments_in_sector(fs: FactSheet, sector: str) -> list[str]:
    """Corners whose apex, and straights whose midpoint, lie inside the sector (by distance)."""
    v = {f.id: f.value for f in fs.facts}
    lo, hi = v.get(f"{sector}.start_m"), v.get(f"{sector}.end_m")
    if lo is None or hi is None:
        return []
    out: list[str] = []
    for seg in fs.segments:
        p = seg.fact_prefix
        if seg.kind == "corner":
            at = v.get(f"{p}.apex_m")
        elif seg.kind == "straight":
            a, b = v.get(f"{p}.start_m"), v.get(f"{p}.end_m")
            at = None if a is None or b is None else (float(a) + float(b)) / 2
        else:
            continue
        if at is not None and float(lo) <= float(at) < float(hi):
            out.append(p)
    return out


def build_slice(intent: Intent, fs: FactSheet, entities: Entities) -> Slice:
    lap = [f"LAP.{x}" for x in LAP_FIELDS]
    drivers = ["target.driver", "reference.driver", "target.lap_number", "reference.lap_number"]
    segments: list[str] = []
    evidence: Evidence | None = None
    if intent is Intent.WHERE_LOST_TIME:
        segments = list(dict.fromkeys([*fs.rankings.largest_losses[:TOP], *fs.rankings.largest_gains[:TOP]]))
        ids = lap + drivers + [i for p in segments for i in _segment_ids(p)]
    elif intent is Intent.WHY_FASTER_IN_SECTOR:
        sector = (entities.sectors or [""])[0]
        n = sector.removeprefix("SEC")
        inside = segments_in_sector(fs, sector) if sector else []
        losses = {s.fact_prefix: abs(s.time_loss_s or 0.0) for s in fs.segments}
        segments = sorted(inside, key=lambda p: losses.get(p, 0.0), reverse=True)[:6]
        ids = [f"LAP.sector_{n}_delta_s", f"{sector}.time_loss_s", f"{sector}.official_sector_delta_s",
               f"target.sector_{n}_s", f"reference.sector_{n}_s", f"{sector}.label", f"{sector}.confidence"]
        ids += drivers + [i for p in segments for i in _segment_ids(p)]
    elif intent is Intent.TIRE_STATUS:
        ids = [f.id for f in fs.facts if f.id.startswith("tire.")]
        ids += [c.id for c in fs.categorical_facts if c.id.startswith("tire.")]
        ids += ["target.compound", "target.tyre_age_laps", "target.fresh_tyre", "target.driver"]
    elif intent is Intent.FOCUS_CORNERS:
        segments = fs.rankings.largest_losses[:5]
        ids = ["LAP.time_loss_s", *drivers] + [i for p in segments for i in _segment_ids(p)]
    elif intent is Intent.WHEN_TO_PIT:
        ids = [f"tire.{x}" for x in (*PIT_FIELDS, *PIT_CATS)] + ["tire.compound", "tire.tyre_age_laps",
                                                                   "target.driver"]
    elif intent is Intent.CAUSE_ATTRIBUTION:
        evidence = build_evidence(fs, entities.segments or None)
        segments = evidence.scope
        ids = evidence.fact_ids() + ["target.compound", "reference.compound", "target.tyre_age_laps",
                                     "reference.tyre_age_laps", "target.track_status_flags",
                                     "reference.track_status_flags", "target.driver", "reference.driver",
                                     "data_quality.notes", "session.notes", TRAFFIC_FACT]
    elif intent is Intent.CORNER_DETAIL:
        segments = entities.corners or entities.straights or entities.sectors
        prefixes = tuple(f"{p}." for p in segments[:2])
        ids = [f.id for f in fs.facts if f.id.startswith(prefixes) and f.id.split(".", 1)[1] not in GEOMETRY]
        ids += [f"{p}.{x}" for p in segments[:2] for x in (*SEGMENT_CATS, "corner_type")] + drivers
    else:
        ids = []
    numeric, cats = _keep(fs, ids)
    return Slice(intent, numeric, cats, segments, evidence)
