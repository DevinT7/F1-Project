"""Phase 7A: build the FactSheet from verified findings (Phase 3) and tyre facts (Phase 6E/6F).

    build_factsheet(findings, tire_prediction | None, context) -> FactSheet

Every value of both sources becomes a fact with a stable id "{prefix}.{field}" (prefixes: LAP,
T1…, STR1…, SEC1…, target, reference, session, quality, rankings, data_quality, tire) or is
excluded on purpose (FINDINGS_EXCLUDED; TIRE_NOT_EXPOSED mirrors docs/tire_facts_contract.md).
tests/test_ai_factsheet.py walks both source documents and fails on any unmapped value.

Meanings state the sign convention in words. An unknown metric key raises (no generic
fallback), so a new findings field cannot reach the AI without an explicit meaning.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..processing.insights import Finding, FindingsReport
from .numbers import display_forms
from .schemas import (
    CategoricalFact, ConditionsSection, DataQualitySection, DriverSection, DriversSection, Fact, FactSheet,
    LapSummarySection, RankingsSection, SegmentSection, SessionSection,
)

CAUSES = ["early_braking_loss", "late_braking_gain", "low_min_speed", "high_min_speed", "late_throttle",
          "early_throttle", "lower_exit_speed", "higher_exit_speed", "straight_line_deficit",
          "straight_line_advantage", "unclear"]

# findings leaves that are deliberately NOT facts (JSON-pointer regexes)
FINDINGS_EXCLUDED: tuple[str, ...] = (
    r"/schema_version",                                              # document format, not about the race
    r"/comparison_id",                                               # identifier; the session/driver facts carry it
    r"/(lap_summary|corners/\d+|straights/\d+|sectors/\d+)/(id|kind)",  # identity is the fact prefix itself
)
# tyre leaves that are not facts: exactly the contract's "Not exposed as facts" table
TIRE_NOT_EXPOSED: tuple[str, ...] = (
    r"/predicted_laps", r"/predicted_laps/[1-9]\d*/(lap|pred_s|lo_s|hi_s)",
    r"/predicted_laps/0/lap",                                         # always tire.as_of_lap + 1
    r"/history", r"/history/\d+/\w+",
    r"/threshold_curve", r"/threshold_curve/\d+/\w+",
    r"/warnings",
    r"/pit_window/threshold_pace_s", r"/pit_window/best_pace_s", r"/pit_window/notes",
)

# ---------------------------------------------------------------- units and meanings
UNIT_BY_KEY: dict[str, str] = {
    "gear_min": "gear", "gear_at_apex": "gear", "gear_min_diff": "gear", "corner_count": "count",
    "corners_lost": "count", "corners_gained": "count", "corners_unclear": "count", "drs_open_fraction": "fraction",
    "drs_open_fraction_diff": "fraction", "length_scale_factor": "ratio", "sample_count_ref": "count",
    "sample_count_target": "count", "repairs": "count", "dropped_glitches": "count", "rank": "rank",
    "corner_number": "corner", "lap_number": "lap", "tyre_age_laps": "laps",
}


def unit_for(key: str) -> str:
    if key in UNIT_BY_KEY:
        return UNIT_BY_KEY[key]
    for suffix, unit in (("_kph", "km/h"), ("_ms2", "m/s²"), ("_c", "°C"), ("_s", "s"), ("_m", "m")):
        if key.endswith(suffix):
            return unit
    raise KeyError(f"no unit for field {key!r}")


# target − reference differences: (what, meaning of a POSITIVE value)
METRIC_MEANINGS: dict[str, tuple[str, str]] = {
    "braking_start_diff_m": ("braking point, target − reference", "target started braking later (further along the lap)"),
    "braking_duration_diff_s": ("braking duration, target − reference", "target braked for longer"),
    "entry_speed_diff_kph": ("entry speed, target − reference", "target carried more speed into the {kind}"),
    "min_speed_diff_kph": ("minimum speed, target − reference", "target was faster at the slowest point"),
    "exit_speed_diff_kph": ("exit speed, target − reference", "target left the {kind} faster"),
    "throttle_reapply_diff_m": ("throttle re-application point, target − reference",
                                "target got back on the throttle later"),
    "time_to_full_throttle_diff_s": ("time from throttle pick-up to full throttle, target − reference",
                                     "target took longer to reach full throttle"),
    "gear_min_diff": ("lowest gear used, target − reference", "target used a higher minimum gear"),
    "top_speed_diff_kph": ("top speed, target − reference", "target reached a higher top speed"),
    "drs_open_fraction_diff": ("share of the straight with DRS open, target − reference (0–1)",
                               "target had DRS open for a larger share"),
    "time_diff_s": ("time over the straight, target − reference", "target was slower"),
    "total_delta_s": ("whole-lap time difference from the aligned telemetry, target − reference", "target was slower"),
    "official_gap_s": ("official lap-time gap, target − reference", "target was slower"),
    "sector_1_delta_s": ("official sector 1 time difference, target − reference", "target was slower"),
    "sector_2_delta_s": ("official sector 2 time difference, target − reference", "target was slower"),
    "sector_3_delta_s": ("official sector 3 time difference, target − reference", "target was slower"),
    "official_sector_delta_s": ("official sector time difference, target − reference", "target was slower"),
    "boundary_discrepancy_s": ("aligned-telemetry delta minus the official cumulative gap at the sector boundary",
                               "the telemetry delta overstates the official gap there"),
}
# counts on the lap summary (not differences)
COUNT_MEANINGS: dict[str, str] = {
    "corner_count": "number of corners analysed",
    "corners_lost": "corners where the target lost at least the noise floor (0.02 s)",
    "corners_gained": "corners where the target gained at least the noise floor (0.02 s)",
    "corners_unclear": "corners where no rule explained the time difference (cause 'unclear')",
}
# absolute values of one driver
ABSOLUTE_MEANINGS: dict[str, str] = {
    "braking_start_m": "distance from the start line where {who} started braking",
    "braking_duration_s": "how long {who} braked",
    "peak_decel_ms2": "{who}'s peak deceleration while braking (informational)",
    "entry_speed_kph": "{who}'s speed at the start of the segment",
    "min_speed_kph": "{who}'s minimum speed in the segment",
    "min_speed_m": "distance from the start line of {who}'s minimum speed",
    "exit_speed_kph": "{who}'s speed at the end of the segment",
    "throttle_reapply_m": "distance from the start line where {who} got back on the throttle",
    "full_throttle_m": "distance from the start line where {who} reached full throttle",
    "time_to_full_throttle_s": "time {who} took from throttle pick-up to full throttle",
    "gear_min": "{who}'s lowest gear in the segment",
    "gear_at_apex": "{who}'s gear at the apex",
    "top_speed_kph": "{who}'s top speed",
    "top_speed_m": "distance from the start line of {who}'s top speed",
    "drs_open_fraction": "share of the straight {who} had DRS open (0–1)",
    "time_s": "{who}'s time over the segment",
    "lap_time_s": "{who}'s lap time",
}
NULL_REASONS: dict[str, str] = {
    "braking": "no braking detected in this segment",
    "peak_decel": "no braking detected in this segment",
    "throttle": "no throttle re-application detected in this segment",
    "full_throttle": "full throttle was not reached in this segment",
    "time_to_full_throttle": "full throttle was not reached in this segment",
}


def null_reason(key: str) -> str:
    for stem, reason in NULL_REASONS.items():
        if stem in key:
            return reason
    return "not measured for this segment"


# each difference metric and the per-driver value it is computed from
DIFF_OF: dict[str, str] = {
    "braking_start_diff_m": "braking_start_m", "braking_duration_diff_s": "braking_duration_s",
    "entry_speed_diff_kph": "entry_speed_kph", "min_speed_diff_kph": "min_speed_kph",
    "exit_speed_diff_kph": "exit_speed_kph", "throttle_reapply_diff_m": "throttle_reapply_m",
    "time_to_full_throttle_diff_s": "time_to_full_throttle_s", "gear_min_diff": "gear_min",
    "top_speed_diff_kph": "top_speed_kph", "drs_open_fraction_diff": "drs_open_fraction", "time_diff_s": "time_s",
}


def diff_null_reason(key: str, f: Finding, target: str, reference: str) -> str:
    """A difference is null when either side is missing: say which side, so it is not misread."""
    base = null_reason(key)
    source = DIFF_OF.get(key)
    if source is None:
        return base
    t_missing, r_missing = f.target.get(source) is None, f.reference.get(source) is None
    what = base.replace(" in this segment", "")
    if t_missing and not r_missing:
        return f"{what} for the target ({target}) only; the reference ({reference}) has a value"
    if r_missing and not t_missing:
        return f"{what} for the reference ({reference}) only; the target ({target}) has a value"
    return f"{what} for either driver" if (t_missing and r_missing) else base


# ---------------------------------------------------------------- tyre facts (docs/tire_facts_contract.md)
# id → (source pointer in TirePrediction, unit or None for categorical, meaning)
TIRE_FACTS: list[tuple[str, str, str | None, str]] = [
    ("tire.session_key", "/session_key", None, "session of the tyre forecast"),
    ("tire.driver", "/driver", None, "driver of the tyre forecast"),
    ("tire.as_of_lap", "/lap", "lap", "the forecast uses only laps up to and including this lap"),
    ("tire.horizon_laps", "/horizon", "laps", "number of laps forecast"),
    ("tire.status", "/status", None, "ok, or insufficient_data (see tire.reason_code)"),
    ("tire.reason_code", "/reason_code", None, "why there is no forecast"),
    ("tire.reason", "/reason", None, "why there is no forecast, in words"),
    ("tire.clean_laps_required", "/clean_laps_required", "laps", "clean laps a stint needs before a forecast is made"),
    ("tire.race_laps_total", "/race_laps_total", "laps", "scheduled race distance"),
    ("tire.stint", "/stint", "count", "stint number of the driver at the as-of lap"),
    ("tire.compound", "/compound", None, "tyre compound of the current stint"),
    ("tire.tyre_age_laps", "/tyre_age", "laps", "laps on the current set at the as-of lap"),
    ("tire.clean_laps_used", "/clean_laps_used", "laps", "clean laps of the stint the forecast used"),
    ("tire.rainfall_in_stint", "/rainfall_in_stint", None, "rain was reported during the stint (up to the as-of lap)"),
    ("tire.deg_s_per_lap", "/degradation_s_per_lap", "s/lap",
     "combined adjusted trend: how much slower each lap gets as the tyre ages after removing the fuel effect "
     "(includes track evolution; not pure tyre wear); positive = getting slower"),
    ("tire.deg_ci_low", "/degradation_ci/0", "s/lap", "lower end of the 95% interval of tire.deg_s_per_lap"),
    ("tire.deg_ci_high", "/degradation_ci/1", "s/lap", "upper end of the 95% interval of tire.deg_s_per_lap"),
    ("tire.next_lap_pred_s", "/predicted_laps/0/pred_s", "s", "forecast lap time of the next lap"),
    ("tire.next_lap_lo_s", "/predicted_laps/0/lo_s", "s", "low end of the 80% prediction interval of the next lap"),
    ("tire.next_lap_hi_s", "/predicted_laps/0/hi_s", "s", "high end of the 80% prediction interval of the next lap"),
    ("tire.window_earliest_lap", "/pit_window/earliest_lap", "lap",
     "earliest lap of the heuristic pit window (pessimistic forecast reaches best pace + X)"),
    ("tire.window_central_lap", "/pit_window/central_lap", "lap",
     "central lap of the heuristic pit window (the forecast reaches best pace + X)"),
    ("tire.window_latest_lap", "/pit_window/latest_lap", "lap",
     "latest lap of the heuristic pit window; null means no crossing within the horizon (open-ended)"),
    ("tire.threshold_s", "/pit_window/threshold_s", "s", "X: seconds above best pace treated as 'tyre gone' (a heuristic)"),
    ("tire.window_pace_basis", "/pit_window/pace_basis", None, "whether pace was fuel-adjusted for the window"),
    ("tire.confidence", "/pit_window/confidence", None, "confidence of the pit window"),
    ("tire.confidence_reasons", "/pit_window/confidence_reasons", None, "why the window has that confidence"),
    ("tire.window_assumptions", "/pit_window/assumptions", None, "assumptions that must accompany any window statement"),
    ("tire.window_method", "/pit_window/method", None, "pit-window algorithm"),
    ("tire.window_reason", "/pit_window_reason", None, "why there is no pit window"),
    ("tire.remaining_competitive_laps", "/remaining_competitive_laps", "laps",
     "laps from the as-of lap to the central pit lap"),
    ("tire.model_name", "/model/name", None, "tyre model name"),
    ("tire.model_version", "/model/version", None, "tyre model version"),
    ("tire.model_trained_through", "/model/trained_through", None, "date of the last race in the model's training data"),
]


def tire_null_reason(fact_id: str, t: dict[str, Any]) -> str:
    """Why a tyre fact is null, per docs/tire_facts_contract.md (a null window lap is a statement)."""
    if t.get("status") == "insufficient_data" and fact_id not in (
            "tire.session_key", "tire.driver", "tire.as_of_lap", "tire.horizon_laps", "tire.status",
            "tire.clean_laps_required", "tire.race_laps_total", "tire.reason_code", "tire.reason"):
        return f"no forecast for this lap: {t.get('reason') or t.get('reason_code')}"
    if fact_id in ("tire.reason_code", "tire.reason"):
        return "not applicable: the forecast is available"
    window = t.get("pit_window")
    if fact_id.startswith(("tire.window_", "tire.confidence", "tire.threshold_s")) or fact_id == "tire.remaining_competitive_laps":
        if fact_id == "tire.window_reason":
            return "not applicable: a pit window exists"
        if window is None:
            return f"no pit window: {(t.get('pit_window_reason') or 'unavailable').replace('_', ' ')}"
        if fact_id == "tire.window_latest_lap":
            return ("no crossing within the forecast horizon: the window is open-ended "
                    "(the optimistic forecast never reaches the threshold); this is not missing data")
        if fact_id in ("tire.window_central_lap", "tire.remaining_competitive_laps"):
            return "the forecast never reaches the threshold within the horizon"
        if fact_id == "tire.window_earliest_lap":
            return "even the pessimistic forecast never reaches the threshold within the horizon"
    if fact_id in ("tire.deg_ci_low", "tire.deg_ci_high"):
        return "the model gives no confidence interval for degradation"
    if fact_id == "tire.deg_s_per_lap":
        return "the model has no degradation term"
    if fact_id.startswith("tire.model_"):
        return "no model answered"
    return "not given for this forecast"


class FactSheetContext(BaseModel):
    """What the findings and the tyre response do not carry."""
    model_config = ConfigDict(extra="forbid")
    session_date: str | None = Field(default=None, description="ISO date of the session")
    teams: dict[str, str] = Field(default_factory=dict, description="driver code → team")
    tire_unavailable_reason: str | None = Field(default=None, description="why there is no tyre forecast (when tire is None)")


# ---------------------------------------------------------------- the builder
class _Builder:
    def __init__(self, findings: dict[str, Any], tire: dict[str, Any] | None) -> None:
        self.docs = {"findings": findings, "tire": tire}
        self.facts: list[Fact] = []
        self.categorical: list[CategoricalFact] = []
        self.claimed: set[str] = set()

    def _resolve(self, root: str, pointer: str) -> tuple[bool, Any, str]:
        """(found, value, source) — when a parent is null, the first fact claiming it takes the null."""
        node: Any = self.docs[root]
        walked = ""
        for part in pointer.strip("/").split("/"):
            if node is None:
                source = f"{root}:{walked}"
                if source in self.claimed:
                    return False, None, f"derived:{root}:{pointer} (parent {walked or '/'} is null)"
                return False, None, source
            walked += f"/{part}"
            if isinstance(node, list):
                idx = int(part)
                node = node[idx] if idx < len(node) else None
            else:
                node = node.get(part) if isinstance(node, dict) else None
        return True, node, f"{root}:{pointer}"

    def _source(self, root: str, pointer: str) -> tuple[Any, str]:
        """Resolve, but never claim a value the contract excludes (e.g. a null predicted_laps)."""
        _found, value, source = self._resolve(root, pointer)
        if root == "tire" and source.startswith("tire:") and any(
                re.fullmatch(p, source.removeprefix("tire:")) for p in TIRE_NOT_EXPOSED):
            source = f"derived:tire:{pointer} (parent not exposed and null)"
        return value, source

    def number(self, fact_id: str, root: str, pointer: str, unit: str, meaning: str, null_why: str) -> None:
        value, source = self._source(root, pointer)
        self.claimed.add(source)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise TypeError(f"{fact_id}: {value!r} is not a number")
        self.facts.append(Fact(id=fact_id, value=value, unit=unit, display=display_forms(value, unit),
                               meaning=meaning if value is not None else f"unavailable — {null_why}", source=source))

    def category(self, fact_id: str, root: str, pointer: str, meaning: str, allowed: list[str] | None = None,
                 null_meaning: str | None = None) -> None:
        value, source = self._source(root, pointer)
        self.claimed.add(source)
        if value is None:
            meaning = f"unavailable — {null_meaning}" if null_meaning else f"unavailable — {meaning}"
        self.categorical.append(CategoricalFact(id=fact_id, value=value, allowed=allowed, meaning=meaning, source=source))

    def null_tire(self, reason: str) -> None:
        for fact_id, _ptr, unit, meaning in TIRE_FACTS:
            src = f"derived:tyre forecast unavailable ({reason})"
            if unit:
                self.facts.append(Fact(id=fact_id, value=None, unit=unit, display=[],
                                       meaning=f"unavailable — {reason}", source=src))
            else:
                self.categorical.append(CategoricalFact(id=fact_id, value=None, meaning=f"unavailable — {reason}",
                                                        source=src))


def _prefix(f: Finding) -> str:
    return f.id.rsplit(":", 1)[1]


def _segment_facts(b: _Builder, f: Finding, pointer: str, target: str, reference: str) -> None:
    p = _prefix(f)
    loss_meaning = {
        "time_loss_s": f"time the target lost to the reference over {f.label} (positive = target slower, negative = faster)",
        "entry_loss_s": f"time lost from the start of {f.label} to its apex (positive = target slower)",
        "exit_loss_s": f"time lost from the apex of {f.label} to its end (positive = target slower)",
    }
    for key, meaning in loss_meaning.items():
        b.number(f"{p}.{key}", "findings", f"{pointer}/{key}", "s", meaning, "not computed for this segment")
    b.number(f"{p}.rank", "findings", f"{pointer}/rank", "rank",
             f"rank of {f.label} by absolute time difference among corners (1 = largest)", "only corners are ranked")
    b.number(f"{p}.corner_number", "findings", f"{pointer}/corner_number", "corner",
             f"official corner number of {f.label}", "not a numbered corner")
    for key in ("start_m", "end_m", "apex_m"):
        what = {"start_m": "start", "end_m": "end", "apex_m": "apex"}[key]
        b.number(f"{p}.{key}", "findings", f"{pointer}/segment/{key}", "m",
                 f"distance from the start line of the {what} of {f.label}", "no apex for this segment")
    for key in f.metrics:
        if key in METRIC_MEANINGS:
            what, positive = METRIC_MEANINGS[key]
            kind = {"corner": "corner", "straight": "straight"}.get(f.kind, "segment")
            meaning = f"{f.label}: {what} (positive = {positive.format(kind=kind)})"
        elif key in COUNT_MEANINGS:
            meaning = COUNT_MEANINGS[key]
        else:
            raise KeyError(f"no meaning for metric {key!r}: add it to METRIC_MEANINGS")
        b.number(f"{p}.{key}", "findings", f"{pointer}/metrics/{key}", unit_for(key), meaning,
                 diff_null_reason(key, f, target, reference))
    for side, who in (("target", target), ("reference", reference)):
        for key in getattr(f, side):
            if key not in ABSOLUTE_MEANINGS:
                raise KeyError(f"no meaning for {side} value {key!r}: add it to ABSOLUTE_MEANINGS")
            b.number(f"{p}.{side}_{key}", "findings", f"{pointer}/{side}/{key}", unit_for(key),
                     f"{f.label}: " + ABSOLUTE_MEANINGS[key].format(who=f"{who} ({side})"), null_reason(key))
    b.category(f"{p}.label", "findings", f"{pointer}/label", "display name of the segment")
    b.category(f"{p}.causes", "findings", f"{pointer}/causes",
               "triggered rules (contributing factors, not proof); the only causes a report may name", allowed=CAUSES)
    b.category(f"{p}.confidence", "findings", f"{pointer}/confidence", "confidence of this finding",
               allowed=["high", "medium", "low"])
    b.category(f"{p}.notes", "findings", f"{pointer}/notes", "qualifications of this finding")
    b.category(f"{p}.corner_type", "findings", f"{pointer}/corner_type", "corner type by the reference's minimum speed",
               allowed=["slow", "medium", "fast"])
    b.category(f"{p}.apex_source", "findings", f"{pointer}/apex_source", "how the apex was located")


def _driver_facts(b: _Builder, side: str, ctx_pointer: str, sector_count: int) -> None:
    who = side
    b.number(f"{side}.lap_number", "findings", f"{ctx_pointer}/lap_number", "lap", f"lap number of the {who} lap", "unknown")
    b.number(f"{side}.lap_time_s", "findings", f"{ctx_pointer}/lap_time_s", "s", f"official lap time of the {who} lap", "unknown")
    for i in range(sector_count):
        b.number(f"{side}.sector_{i + 1}_s", "findings", f"{ctx_pointer}/sector_s/{i}", "s",
                 f"official sector {i + 1} time of the {who} lap", "no official sector time")
    b.number(f"{side}.tyre_age_laps", "findings", f"{ctx_pointer}/tyre_age_laps", "laps",
             f"age of the {who}'s tyres on that lap", "tyre age unknown")
    b.number(f"{side}.air_temp_c", "findings", f"{ctx_pointer}/air_temp_c", "°C", f"air temperature during the {who} lap",
             "no weather sample")
    b.number(f"{side}.track_temp_c", "findings", f"{ctx_pointer}/track_temp_c", "°C",
             f"track temperature during the {who} lap", "no weather sample")
    b.category(f"{side}.driver", "findings", f"{ctx_pointer}/driver", f"driver code of the {who}")
    b.category(f"{side}.compound", "findings", f"{ctx_pointer}/compound", f"tyre compound of the {who} lap")
    b.category(f"{side}.fresh_tyre", "findings", f"{ctx_pointer}/fresh_tyre", f"the {who} lap was on a new set")
    b.category(f"{side}.rainfall", "findings", f"{ctx_pointer}/rainfall", f"rain reported during the {who} lap")
    b.category(f"{side}.track_status", "findings", f"{ctx_pointer}/track_status", "FastF1 track-status codes on the lap")
    b.category(f"{side}.track_status_flags", "findings", f"{ctx_pointer}/track_status_flags", "track-status flags on the lap")


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def factsheet_hash(fs: FactSheet) -> str:
    return hashlib.sha256(canonical_json(fs.model_dump(mode="json", exclude={"factsheet_hash"})).encode("utf-8")).hexdigest()


def build_factsheet(findings: FindingsReport, tire: Any | None, context: FactSheetContext = FactSheetContext()) -> FactSheet:
    """FactSheet from verified findings + the tyre response (TirePrediction JSON/model, or None)."""
    fdoc = findings.model_dump(mode="json")
    tdoc: dict[str, Any] | None = None
    if tire is not None:
        tdoc = tire if isinstance(tire, dict) else tire.model_dump(mode="json")
    b = _Builder(fdoc, tdoc)
    sc = findings.session_context
    tgt, ref = sc.target.driver, sc.reference.driver

    # session + drivers
    for fid, key, meaning in (("session.key", "session_key", "session identifier"), ("session.event", "event_name", "event"),
                              ("session.name", "session_name", "session name"),
                              ("session.code", "session_code", "session type code (Q, R, …)"),
                              ("session.notes", "notes", "context qualifications (e.g. traffic data unavailable)")):
        b.category(fid, "findings", f"/session_context/{key}", meaning)
    for side in ("target", "reference"):
        _driver_facts(b, side, f"/session_context/{side}", len(getattr(sc, side).sector_s))

    # segments
    b_ptr = [("/lap_summary", findings.lap_summary)]
    b_ptr += [(f"/corners/{i}", f) for i, f in enumerate(findings.corners)]
    b_ptr += [(f"/straights/{i}", f) for i, f in enumerate(findings.straights)]
    b_ptr += [(f"/sectors/{i}", f) for i, f in enumerate(findings.sectors)]
    for pointer, f in b_ptr:
        _segment_facts(b, f, pointer, tgt, ref)

    # rankings, quality, notes
    to_prefix = {f.id: _prefix(f) for _, f in b_ptr}
    b.category("rankings.largest_losses", "findings", "/largest_losses", "corner ids where the target lost most, biggest first")
    b.category("rankings.largest_gains", "findings", "/largest_gains", "corner ids where the target gained most, biggest first")
    q = fdoc["quality"]
    quality_meanings = {
        "sample_count_ref": "telemetry samples in the reference lap", "sample_count_target": "telemetry samples in the target lap",
        "length_scale_factor": "length normalization applied to the target's distance (1 = none)",
        "final_delta_error_s": "difference between the aligned delta at the line and the official gap (should be 0)",
        "repairs": "telemetry samples repaired", "dropped_glitches": "telemetry glitches dropped",
        "tiling_error_s": "corner losses' sum minus the lap delta (should be 0)",
        "sector_check_max_discrepancy_s": "largest disagreement between the aligned delta and official sector times",
        "xy_correction_rms_m": "size of the X/Y position correction applied to the target's distance",
    }
    for key in q:
        if key in ("sector_check_ok", "alignment"):
            continue
        b.number(f"quality.{key}", "findings", f"/quality/{key}", unit_for(key), quality_meanings[key], "not computed")
    b.category("quality.sector_check_ok", "findings", "/quality/sector_check_ok",
               "aligned delta agrees with official sector times within 0.03 s")
    b.category("quality.alignment", "findings", "/quality/alignment", "how the laps were aligned", allowed=["xy", "distance"])
    b.category("data_quality.notes", "findings", "/notes", "qualifications of the whole comparison")

    # tyre
    if tdoc is None:
        reason = context.tire_unavailable_reason or "no tyre forecast was requested for this comparison"
        b.null_tire(reason)
        tire_section: dict[str, Any] = {"status": "unavailable", "reason": reason}
    else:
        for fact_id, ptr, unit, meaning in TIRE_FACTS:
            if unit:
                b.number(fact_id, "tire", ptr, unit, meaning, tire_null_reason(fact_id, tdoc))
            else:
                b.category(fact_id, "tire", ptr, meaning, null_meaning=tire_null_reason(fact_id, tdoc))
        values: dict[str, Any] = {f.id: f.value for f in b.facts} | {c.id: c.value for c in b.categorical}
        tire_section = {fid.removeprefix("tire."): values.get(fid) for fid, *_ in TIRE_FACTS}

    def val(fid: str) -> Any:
        return next(f.value for f in b.facts if f.id == fid)

    segments = [SegmentSection(label=f.label, fact_prefix=_prefix(f), kind=f.kind,  # type: ignore[arg-type]
                               time_loss_s=f.time_loss_s, confidence=f.confidence, causes=list(f.causes), rank=f.rank)
                for _, f in b_ptr[1:]]
    notes = list(findings.notes) + list(sc.notes) + [f"{_prefix(f)}: {n}" for _, f in b_ptr for n in f.notes]
    flags = sorted(set(sc.target.track_status_flags) | set(sc.reference.track_status_flags))
    fs = FactSheet(
        session=SessionSection(key=(sc.session_key or "").replace("_", "-") or None, event=sc.event_name,
                               session_type=sc.session_code, session_name=sc.session_name, date=context.session_date),
        drivers=DriversSection(
            target=DriverSection(code=tgt, team=context.teams.get(tgt), lap=sc.target.lap_number, lap_time_s=sc.target.lap_time_s),
            reference=DriverSection(code=ref, team=context.teams.get(ref), lap=sc.reference.lap_number,
                                    lap_time_s=sc.reference.lap_time_s)),
        lap_summary=LapSummarySection(
            total_delta_s=val("LAP.total_delta_s"),
            sector_deltas_s=[val(f"LAP.sector_{i}_delta_s") for i in (1, 2, 3)],
            top_speed_diff_kph=val("LAP.top_speed_diff_kph")),
        segments=segments,
        rankings=RankingsSection(largest_losses=[to_prefix[i] for i in findings.largest_losses],
                                 largest_gains=[to_prefix[i] for i in findings.largest_gains]),
        tire=tire_section,
        conditions=ConditionsSection(track_temp_c=sc.target.track_temp_c, air_temp_c=sc.target.air_temp_c,
                                     track_status_flags=flags),
        data_quality=DataQualitySection(
            low_confidence_segments=[s.fact_prefix for s in segments if s.confidence == "low"], notes=notes),
        facts=b.facts, categorical_facts=b.categorical)
    return fs.model_copy(update={"factsheet_hash": factsheet_hash(fs)})


def factsheet_from_inputs(inputs: dict[str, Any]) -> FactSheet:
    """Fixture form: {"findings": FindingsReport JSON, "tire": TirePrediction JSON | null, "context": {...}}."""
    return build_factsheet(FindingsReport.model_validate(inputs["findings"]), inputs.get("tire"),
                           FactSheetContext.model_validate(inputs.get("context", {})))
