"""Findings: per-corner metrics, triggered-rule attribution, confidence and ranking.

generate_findings(comparison, corners) turns a Phase 2 LapComparison into a
FindingsReport — the "verified findings" JSON the AI layer explains. The Pydantic
models here are the canonical schema (docs/insight_schema.md is generated from them
and a test keeps the two in sync).

Conventions: time_loss_s > 0 means the target lost time to the reference; every
*_diff is target − reference; missing values are null, never 0; causes are rules
whose thresholds were met ("contributing factors"), never inferred explanations.

    python -m app.processing.insights        # print the JSON schema
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from ..ingestion.models import CornerInfo, FloatArray, LapRecord, SessionInfo
from .config import Thresholds
from .corner_metrics import corner_lap_metrics, diff, straight_lap_metrics
from .delta import sector_check
from .errors import InsufficientDataError
from .models import AlignedLap, LapComparison, SectorDiagnostic
from .segments import ApexSource, CornerSegment, CornerType, build_corner_segments, corner_type, detect_straights

SCHEMA_VERSION = 1
TILING_TOLERANCE_S = 1e-9

FindingKind = Literal["corner", "straight", "sector", "lap_summary"]
Confidence = Literal["high", "medium", "low"]
Cause = Literal[
    "early_braking_loss", "late_braking_gain",
    "low_min_speed", "high_min_speed",
    "late_throttle", "early_throttle",
    "lower_exit_speed", "higher_exit_speed",
    "straight_line_deficit", "straight_line_advantage",
    "unclear",
]
Value = float | int | None

TRACK_STATUS_FLAGS: dict[str, str] = {
    "1": "green", "2": "yellow", "3": "unknown_3", "4": "safety_car",
    "5": "red", "6": "vsc_deployed", "7": "vsc_ending",
}


# ---------------------------------------------------------------- schema
class SegmentSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_m: float
    end_m: float
    apex_m: float | None = None


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(description='"<session>:<TARGET>-vs-<REFERENCE>:<code>", e.g. "2024-05-Q:ALO-vs-VER:T7"')
    kind: FindingKind
    label: str
    segment: SegmentSpan | None = None
    time_loss_s: float | None = Field(default=None, description="target − reference over the segment; > 0 = target lost time")
    entry_loss_s: float | None = Field(default=None, description="corners: loss from segment start to apex")
    exit_loss_s: float | None = Field(default=None, description="corners: loss from apex to segment end")
    metrics: dict[str, Value] = Field(default_factory=dict, description="differences, target − reference")
    target: dict[str, Value] = Field(default_factory=dict)
    reference: dict[str, Value] = Field(default_factory=dict)
    causes: list[Cause] = Field(default_factory=list, description="triggered rules: contributing factors, not proof")
    rank: int | None = Field(default=None, description="corners only: 1 = largest |time_loss_s|; ties by distance")
    confidence: Confidence
    notes: list[str] = Field(default_factory=list)
    corner_number: int | None = None
    corner_type: CornerType | None = None
    apex_source: ApexSource | None = None


class LapContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver: str
    lap_number: int
    lap_time_s: float
    sector_s: list[float | None]
    compound: str | None = None
    tyre_age_laps: int | None = None
    fresh_tyre: bool | None = None
    air_temp_c: float | None = None
    track_temp_c: float | None = None
    rainfall: bool | None = None
    track_status: str | None = None
    track_status_flags: list[str] = Field(default_factory=list)


class SessionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_key: str | None = None
    event_name: str | None = None
    session_name: str | None = None
    session_code: str | None = None
    reference: LapContext
    target: LapContext
    notes: list[str] = Field(default_factory=list)


class ReportQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_count_ref: int
    sample_count_target: int
    length_scale_factor: float
    final_delta_error_s: float
    repairs: int
    dropped_glitches: int
    tiling_error_s: float
    sector_check_ok: bool | None
    sector_check_max_discrepancy_s: float | None
    alignment: Literal["xy", "distance"] = Field(
        "distance", description='"xy": the target distance was corrected by projecting its X/Y position onto the '
                                'reference path (blended 50/50 with the length-normalized distance); "distance": '
                                'length normalization only (X/Y missing or implausible)')
    xy_correction_rms_m: float | None = Field(None, description="RMS of the X/Y distance correction applied, metres")


class FindingsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    comparison_id: str
    session_context: SessionContext
    lap_summary: Finding
    corners: list[Finding] = Field(description="tile the lap in distance order; losses sum to the total")
    straights: list[Finding]
    sectors: list[Finding]
    largest_losses: list[str] = Field(description="corner ids, |loss| ≥ noise floor, biggest first")
    largest_gains: list[str]
    quality: ReportQuality
    notes: list[str] = Field(default_factory=list)

    _segments: list[CornerSegment] = PrivateAttr(default_factory=list)

    @property
    def segments(self) -> list[CornerSegment]:
        """The corner segmentation used (reuse it to compare the swapped pair)."""
        return self._segments


def json_schema() -> dict[str, object]:
    return FindingsReport.model_json_schema()


# ---------------------------------------------------------------- context
def _flags(status: str | None) -> list[str]:
    return sorted({TRACK_STATUS_FLAGS.get(ch, f"unknown_{ch}") for ch in (status or "")})


def _lap_context(record: LapRecord) -> LapContext:
    return LapContext(
        driver=record.driver, lap_number=record.lap_number, lap_time_s=record.lap_time_s or 0.0,
        sector_s=list(record.sector_s), compound=record.compound, tyre_age_laps=record.tyre_age,
        fresh_tyre=record.fresh_tyre, air_temp_c=record.air_temp_c, track_temp_c=record.track_temp_c,
        rainfall=record.rainfall, track_status=record.track_status, track_status_flags=_flags(record.track_status),
    )


def build_session_context(reference: LapRecord, target: LapRecord, info: SessionInfo | None = None) -> SessionContext:
    notes = ["traffic data unavailable: do not attribute time loss to traffic"]
    for lap in (reference, target):
        flags = set(_flags(lap.track_status)) - {"green"}
        if flags:
            notes.append(f"{lap.driver} lap {lap.lap_number} ran under: {', '.join(sorted(flags))}")
    return SessionContext(
        session_key=info.key if info else reference.session_key,
        event_name=info.event_name if info else None,
        session_name=info.session_name if info else None,
        session_code=info.session_code if info else None,
        reference=_lap_context(reference),
        target=_lap_context(target),
        notes=notes,
    )


def _minimal_context(comparison: LapComparison) -> SessionContext:
    def lap(a: AlignedLap) -> LapContext:
        return LapContext(driver=a.driver, lap_number=a.lap_number, lap_time_s=a.lap_time_s, sector_s=list(a.sector_s))

    return SessionContext(
        reference=lap(comparison.reference), target=lap(comparison.target),
        notes=["tyre, weather and track-status context not provided",
               "traffic data unavailable: do not attribute time loss to traffic"],
    )


# ---------------------------------------------------------------- attribution
def _rule(value: Value, threshold: float, low: Cause, high: Cause) -> list[Cause]:
    if value is None:
        return []
    if value <= -threshold:
        return [low]
    if value >= threshold:
        return [high]
    return []


def attribute_corner(metrics: dict[str, Value], time_loss_s: float, thresholds: Thresholds) -> list[Cause]:
    causes: list[Cause] = []
    causes += _rule(metrics.get("braking_start_diff_m"), thresholds.braking_diff_m, "early_braking_loss", "late_braking_gain")
    causes += _rule(metrics.get("min_speed_diff_kph"), thresholds.min_speed_diff_kph, "low_min_speed", "high_min_speed")
    causes += _rule(metrics.get("time_to_full_throttle_diff_s"), thresholds.full_throttle_diff_s, "early_throttle", "late_throttle")
    causes += _rule(metrics.get("exit_speed_diff_kph"), thresholds.exit_speed_diff_kph, "lower_exit_speed", "higher_exit_speed")
    if not causes and abs(time_loss_s) >= thresholds.noise_floor_s:
        causes = ["unclear"]
    return causes


def attribute_straight(metrics: dict[str, Value], time_loss_s: float, thresholds: Thresholds) -> list[Cause]:
    causes = _rule(metrics.get("top_speed_diff_kph"), thresholds.top_speed_diff_kph,
                   "straight_line_deficit", "straight_line_advantage")
    if not causes and abs(time_loss_s) >= thresholds.noise_floor_s:
        causes = ["unclear"]
    return causes


def rank_corner_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Order by |time_loss_s| descending, ties by segment start; assign rank 1..n."""
    def key(f: Finding) -> tuple[float, float]:
        return (-abs(f.time_loss_s or 0.0), f.segment.start_m if f.segment else 0.0)

    return [f.model_copy(update={"rank": i}) for i, f in enumerate(sorted(findings, key=key), start=1)]


# ---------------------------------------------------------------- assembly
def _diffs(target: dict[str, Value], reference: dict[str, Value], names: dict[str, str]) -> dict[str, Value]:
    out: dict[str, Value] = {}
    for out_name, field in names.items():
        t, r = target.get(field), reference.get(field)
        out[out_name] = diff(None if t is None else float(t), None if r is None else float(r))
    return out


CORNER_DIFFS = {
    "braking_start_diff_m": "braking_start_m",
    "entry_speed_diff_kph": "entry_speed_kph",
    "min_speed_diff_kph": "min_speed_kph",
    "exit_speed_diff_kph": "exit_speed_kph",
    "throttle_reapply_diff_m": "throttle_reapply_m",
    "time_to_full_throttle_diff_s": "time_to_full_throttle_s",
    "braking_duration_diff_s": "braking_duration_s",
    "gear_min_diff": "gear_min",
}
STRAIGHT_DIFFS = {
    "top_speed_diff_kph": "top_speed_kph",
    "entry_speed_diff_kph": "entry_speed_kph",
    "exit_speed_diff_kph": "exit_speed_kph",
    "drs_open_fraction_diff": "drs_open_fraction",
    "time_diff_s": "time_s",
}


def _session_label(context: SessionContext) -> str:
    return (context.session_key or "session").replace("_", "-")


def _sample_spacing_m(lap: AlignedLap, speed_kph: float | None) -> float | None:
    if speed_kph is None or lap.sample_count <= 1:
        return None
    return speed_kph / 3.6 * lap.lap_time_s / (lap.sample_count - 1)


def _confidence(time_loss_s: float, *, sparse: bool, fallback: bool, thresholds: Thresholds) -> Confidence:
    if abs(time_loss_s) < thresholds.noise_floor_s or sparse:
        return "low"
    return "medium" if fallback else "high"


def generate_findings(
    comparison: LapComparison,
    corners: Sequence[CornerInfo],
    thresholds: Thresholds = Thresholds(),
    *,
    context: SessionContext | None = None,
    segments: Sequence[CornerSegment] | None = None,
) -> FindingsReport:
    grid, delta = comparison.grid_m, comparison.delta_s
    ref, tgt = comparison.reference, comparison.target
    length = float(grid[-1])
    context = context or _minimal_context(comparison)
    prefix = f"{_session_label(context)}:{tgt.driver or 'TARGET'}-vs-{ref.driver or 'REFERENCE'}"
    segs = list(segments) if segments is not None else build_corner_segments(comparison, corners, thresholds)

    def delta_at(d: float) -> float:
        return float(np.interp(d, grid, delta))

    km = length / 1000.0
    sparse = min(ref.sample_count, tgt.sample_count) / km < thresholds.min_samples_per_km if km > 0 else True
    diag = sector_check(comparison)
    sector_failed = diag.available and not diag.ok
    report_notes = [
        "causes are rules whose thresholds were met (contributing factors), not proof of cause",
        "braking uses the on/off brake channel; brake pressure is not available",
    ]
    if sparse:
        report_notes.append("telemetry is sparse (< %.0f samples/km); all findings are low confidence"
                            % thresholds.min_samples_per_km)
    if sector_failed:
        worst = max(abs(c.discrepancy_s) for c in diag.checks)
        report_notes.append(
            f"sector cross-check: the grid delta differs from official sector times by up to {worst:.3f} s "
            f"at a sector boundary (tolerance {diag.tolerance_s} s); corner-level losses carry that "
            "uncertainty, so corner confidence is capped at medium"
        )

    # ---- corners
    corner_findings: list[Finding] = []
    for seg in segs:
        t_start, t_apex, t_end = delta_at(seg.start_m), delta_at(seg.apex_m), delta_at(seg.end_m)
        loss = t_end - t_start
        r = corner_lap_metrics(grid, ref, seg.start_m, seg.end_m, seg.apex_m, thresholds).as_dict()
        t = corner_lap_metrics(grid, tgt, seg.start_m, seg.end_m, seg.apex_m, thresholds).as_dict()
        metrics = _diffs(t, r, CORNER_DIFFS)
        causes = attribute_corner(metrics, loss, thresholds)
        notes: list[str] = []
        if seg.apex_source == "official":
            notes.append(f"no speed minimum within ±{thresholds.apex_match_m:.0f} m of the official corner; "
                         "apex placed at the official distance")
        elif seg.apex_source == "detected":
            notes.append("speed minimum not in the official corner table (e.g. part of a chicane)")
        if seg.corner_number is not None and not seg.xy_ok:
            where = "unknown" if seg.xy_error_m is None else f"{seg.xy_error_m:.0f} m"
            notes.append(f"official corner X/Y is {where} from the lap at its listed distance "
                         f"(> {thresholds.xy_tolerance_m:.0f} m); corner position uncertain")
        for who, m in (("target", t), ("reference", r)):
            if m["full_throttle_m"] is None:
                notes.append(f"{who} did not reach {thresholds.full_throttle_pct:.0f}% throttle before the next segment")
        brake_diff = metrics["braking_start_diff_m"]
        if brake_diff is not None and abs(brake_diff) >= thresholds.braking_diff_m:
            res = _sample_spacing_m(tgt, None if r["entry_speed_kph"] is None else float(r["entry_speed_kph"]))
            if res is not None and abs(brake_diff) < 2 * res:
                notes.append(f"braking points are resolved to ~{res:.0f} m at this telemetry rate; "
                             f"a {abs(brake_diff):.0f} m difference is near that resolution")
        fallback = seg.apex_source != "matched" or (seg.corner_number is not None and not seg.xy_ok) or sector_failed
        corner_findings.append(Finding(
            id=f"{prefix}:{seg.code}", kind="corner", label=seg.label,
            segment=SegmentSpan(start_m=seg.start_m, end_m=seg.end_m, apex_m=seg.apex_m),
            time_loss_s=loss, entry_loss_s=t_apex - t_start, exit_loss_s=t_end - t_apex,
            metrics=metrics, target=t, reference=r, causes=causes,
            confidence=_confidence(loss, sparse=sparse, fallback=fallback, thresholds=thresholds),
            notes=notes, corner_number=seg.corner_number,
            corner_type=corner_type(None if r["min_speed_kph"] is None else float(r["min_speed_kph"]), thresholds),
            apex_source=seg.apex_source,
        ))

    total = float(delta[-1])
    tiling_error = abs(sum(f.time_loss_s or 0.0 for f in corner_findings) - total)
    if tiling_error > TILING_TOLERANCE_S:
        raise InsufficientDataError(f"corner segments do not tile the lap: losses miss the total by {tiling_error:.3e} s")
    ranks = {f.id: f.rank for f in rank_corner_findings(corner_findings)}
    corner_findings = [f.model_copy(update={"rank": ranks[f.id]}) for f in corner_findings]
    ranked = sorted(corner_findings, key=lambda f: (f.rank or 0))
    largest_losses = [f.id for f in ranked if (f.time_loss_s or 0) >= thresholds.noise_floor_s][: thresholds.top_n]
    largest_gains = [f.id for f in ranked if (f.time_loss_s or 0) <= -thresholds.noise_floor_s][: thresholds.top_n]

    # ---- straights
    straight_findings: list[Finding] = []
    for st in detect_straights(grid, ref, thresholds):
        loss = delta_at(st.end_m) - delta_at(st.start_m)
        r = straight_lap_metrics(grid, ref, st.start_m, st.end_m).as_dict()
        t = straight_lap_metrics(grid, tgt, st.start_m, st.end_m).as_dict()
        metrics = _diffs(t, r, STRAIGHT_DIFFS)
        straight_findings.append(Finding(
            id=f"{prefix}:STR{st.index + 1}", kind="straight",
            label=f"Straight {st.index + 1} ({st.start_m:.0f}-{st.end_m:.0f} m)",
            segment=SegmentSpan(start_m=st.start_m, end_m=st.end_m), time_loss_s=loss,
            metrics=metrics, target=t, reference=r, causes=attribute_straight(metrics, loss, thresholds),
            confidence=_confidence(loss, sparse=sparse, fallback=sector_failed, thresholds=thresholds),
        ))

    # ---- sectors
    sector_findings = _sector_findings(diag, grid, delta, prefix, sparse, thresholds)
    if not diag.available:
        report_notes.append("official sector times unavailable for one lap; no sector findings")

    # ---- lap summary
    r_top = straight_lap_metrics(grid, ref, 0.0, length).top_speed_kph
    t_top = straight_lap_metrics(grid, tgt, 0.0, length).top_speed_kph
    summary_metrics: dict[str, Value] = {
        "total_delta_s": total,
        "official_gap_s": tgt.lap_time_s - ref.lap_time_s,
        "sector_1_delta_s": diag.sector_deltas_s[0],
        "sector_2_delta_s": diag.sector_deltas_s[1],
        "sector_3_delta_s": diag.sector_deltas_s[2],
        "top_speed_diff_kph": diff(t_top, r_top),
        "corner_count": len(corner_findings),
        "corners_lost": sum((f.time_loss_s or 0) >= thresholds.noise_floor_s for f in corner_findings),
        "corners_gained": sum((f.time_loss_s or 0) <= -thresholds.noise_floor_s for f in corner_findings),
        "corners_unclear": sum("unclear" in f.causes for f in corner_findings),
    }
    lap_summary = Finding(
        id=f"{prefix}:LAP", kind="lap_summary", label="Lap",
        segment=SegmentSpan(start_m=0.0, end_m=length), time_loss_s=total, metrics=summary_metrics,
        target={"lap_time_s": tgt.lap_time_s, "top_speed_kph": t_top},
        reference={"lap_time_s": ref.lap_time_s, "top_speed_kph": r_top},
        confidence="low" if sparse else "high",
    )

    q = comparison.quality
    report = FindingsReport(
        comparison_id=prefix, session_context=context, lap_summary=lap_summary,
        corners=corner_findings, straights=straight_findings, sectors=sector_findings,
        largest_losses=largest_losses, largest_gains=largest_gains,
        quality=ReportQuality(
            sample_count_ref=q.sample_count_ref, sample_count_target=q.sample_count_target,
            length_scale_factor=q.length_scale_factor, final_delta_error_s=q.final_delta_error_s,
            repairs=q.repairs, dropped_glitches=q.dropped_glitches, tiling_error_s=tiling_error,
            sector_check_ok=diag.ok if diag.available else None,
            sector_check_max_discrepancy_s=max(abs(c.discrepancy_s) for c in diag.checks) if diag.checks else None,
            alignment="xy" if q.alignment == "xy" else "distance",
            xy_correction_rms_m=round(q.xy_correction_rms_m, 3) if q.xy_correction_rms_m is not None else None,
        ),
        notes=report_notes,
    )
    report._segments = segs
    return report


def _sector_findings(
    diag: SectorDiagnostic, grid: FloatArray, delta: FloatArray, prefix: str, sparse: bool, thresholds: Thresholds,
) -> list[Finding]:
    if not diag.available:
        return []
    bounds = [0.0] + [c.boundary_m for c in diag.checks]
    out: list[Finding] = []
    for check, start, end in zip(diag.checks, bounds[:-1], bounds[1:]):
        loss = float(np.interp(end, grid, delta) - np.interp(start, grid, delta))
        official = diag.sector_deltas_s[check.sector - 1]
        notes = [] if check.ok else [
            f"grid delta at the sector line differs from official timing by {check.discrepancy_s:+.3f} s"
        ]
        out.append(Finding(
            id=f"{prefix}:SEC{check.sector}", kind="sector", label=f"Sector {check.sector}",
            segment=SegmentSpan(start_m=start, end_m=end), time_loss_s=loss,
            metrics={"official_sector_delta_s": official, "boundary_discrepancy_s": check.discrepancy_s},
            confidence=_confidence(loss, sparse=sparse, fallback=not check.ok, thresholds=thresholds),
            notes=notes,
        ))
    return out


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, ensure_ascii=False))
