"""Hypothesis strategies for Phase 7C: random but VALID FactSheets.

Findings are built the way the Phase 3 engine builds them: random metrics (signs, magnitudes,
nulls), causes from the engine's own attribute_corner / attribute_straight, the lap delta as the
sum of corner losses, rankings from the losses, and every field the FindingsReport schema needs.
The tyre side is one of: no forecast (with a reason), insufficient_data, a forecast with a pit
window (latest/central possibly open-ended), or a forecast with no window.

Hypothesis draws one seed (plus the shape); every value is generated from it with random.Random.
Drawing each of the ~40 values per corner through Hypothesis made 1,000 examples take ~100 s,
almost all of it in the draw machinery; a seed keeps every example reproducible at a fraction
of the cost.
"""
from __future__ import annotations

import random
from typing import Any

from hypothesis import strategies as st

from app.ai.factsheet import FactSheetContext, build_factsheet
from app.ai.schemas import FactSheet
from app.processing.config import Thresholds
from app.processing.insights import FindingsReport, attribute_corner, attribute_straight

T = Thresholds()
DRIVERS = ["VER", "NOR", "LEC", "PIA", "HAM", "RUS", "ALO", "SAI", "BOT", "ALB"]
TEAMS = {"VER": "Red Bull Racing", "NOR": "McLaren", "LEC": "Ferrari", "PIA": "McLaren", "HAM": "Mercedes",
         "RUS": "Mercedes", "ALO": "Aston Martin", "SAI": "Ferrari", "BOT": "Kick Sauber", "ALB": "Williams"}
NOTE_POOL = [
    "braking points are resolved to ~10 m at this telemetry rate; a 15 m difference is near that resolution",
    "no speed minimum within ±60 m of the official corner; apex placed at the official distance",
    "target did not reach 98% throttle before the next segment",
]


class _Gen:
    """Value generator with the same distributions the per-value strategies had."""

    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)

    def num(self, lo: float, hi: float) -> float:
        k = self.r.random()
        if k < 0.1:
            return 0.0
        if k < 0.2:
            return self.r.uniform(-0.004, 0.004)           # tiny: rounds to zero at some precisions
        return self.r.uniform(lo, hi)

    def maybe(self, value: Any, p_null: float = 0.2) -> Any:
        return None if self.r.random() < p_null else value

    def mnum(self, lo: float, hi: float, p_null: float = 0.2) -> float | None:
        return self.maybe(self.num(lo, hi), p_null)

    def mfloat(self, lo: float, hi: float) -> float | None:
        return self.maybe(self.r.uniform(lo, hi))

    def choice(self, items: list[Any]) -> Any:
        return self.r.choice(items)


def _corner(g: _Gen, k: int, start: float, sid: str) -> dict[str, Any]:
    metrics = {
        "braking_start_diff_m": g.mnum(-40, 40, 0.3), "entry_speed_diff_kph": g.mnum(-30, 30),
        "min_speed_diff_kph": g.mnum(-15, 15), "exit_speed_diff_kph": g.mnum(-20, 20),
        "throttle_reapply_diff_m": g.mnum(-30, 30), "time_to_full_throttle_diff_s": g.mnum(-0.8, 0.8),
        "braking_duration_diff_s": g.mnum(-0.5, 0.5), "gear_min_diff": g.maybe(g.choice([-1.0, 0.0, 1.0])),
    }
    loss = g.num(-0.6, 0.6)

    def side() -> dict[str, Any]:
        return {"braking_start_m": g.mfloat(start, start + 200), "braking_duration_s": g.mfloat(0.2, 3.0),
                "peak_decel_ms2": g.mfloat(5, 50), "entry_speed_kph": g.mfloat(80, 330), "min_speed_kph": g.mfloat(60, 300),
                "min_speed_m": g.mfloat(start, start + 300), "exit_speed_kph": g.mfloat(80, 330),
                "throttle_reapply_m": g.mfloat(start, start + 300), "full_throttle_m": g.mfloat(start, start + 400),
                "time_to_full_throttle_s": g.mfloat(0.1, 4.0), "gear_min": g.maybe(g.r.randint(1, 8)),
                "gear_at_apex": g.maybe(g.r.randint(1, 8))}

    return {"id": f"{sid}:T{k}", "kind": "corner", "label": f"Turn {k}",
            "segment": {"start_m": start, "end_m": start + 300.0, "apex_m": g.maybe(start + 120.0)},
            "time_loss_s": loss, "entry_loss_s": g.mnum(-0.3, 0.3), "exit_loss_s": g.mnum(-0.3, 0.3),
            "metrics": metrics, "target": side(), "reference": side(),
            "causes": attribute_corner(metrics, loss, T), "rank": None, "confidence": g.choice(["high", "medium", "low"]),
            "notes": g.r.sample(NOTE_POOL, g.r.randint(0, 2)), "corner_number": k,
            "corner_type": g.maybe(g.choice(["slow", "medium", "fast"])), "apex_source": g.maybe(g.choice(["matched", "official"]))}


def _straight(g: _Gen, k: int, start: float, sid: str) -> dict[str, Any]:
    metrics = {"top_speed_diff_kph": g.mnum(-12, 12), "entry_speed_diff_kph": g.mnum(-15, 15),
               "exit_speed_diff_kph": g.mnum(-15, 15), "drs_open_fraction_diff": g.mnum(-1, 1),
               "time_diff_s": g.mnum(-0.3, 0.3)}
    loss = g.num(-0.3, 0.3)

    def side() -> dict[str, Any]:
        return {"top_speed_kph": g.mfloat(250, 345), "top_speed_m": g.mfloat(start, start + 900),
                "entry_speed_kph": g.mfloat(80, 300), "exit_speed_kph": g.mfloat(200, 345),
                "drs_open_fraction": g.mfloat(0, 1), "time_s": g.mfloat(3, 20)}

    end = start + 900.0
    return {"id": f"{sid}:STR{k}", "kind": "straight", "label": f"Straight {k} ({start:.0f}-{end:.0f} m)",
            "segment": {"start_m": start, "end_m": end, "apex_m": None}, "time_loss_s": loss, "entry_loss_s": None,
            "exit_loss_s": None, "metrics": metrics, "target": side(), "reference": side(),
            "causes": attribute_straight(metrics, loss, T), "rank": None, "confidence": g.choice(["high", "medium", "low"]),
            "notes": [], "corner_number": None, "corner_type": None, "apex_source": None}


def _tire(g: _Gen, kind: str, target: str, lap: int) -> tuple[dict[str, Any] | None, str | None]:
    if kind == "none":
        return None, g.choice(["tyre forecasting requires a race session; this is qualifying",
                               "tyre model unavailable (HTTP 503: model_not_available)"])
    base: dict[str, Any] = {"status": "ok", "session_key": "2024-99-R", "driver": target, "lap": lap, "horizon": 5,
                            "clean_laps_required": 5, "race_laps_total": 50, "stint": 2,
                            "compound": g.choice(["SOFT", "MEDIUM", "HARD"]), "tyre_age": g.r.randint(1, 30),
                            "rainfall_in_stint": g.r.random() < 0.3, "warnings": [],
                            "model": {"name": "baseline_b_recent_conformal", "version": "2024-07-28.6681d16a",
                                      "trained_through": "2024-07-28"}}
    if kind == "insufficient":
        used = g.r.randint(0, 4)
        return {**base, "status": "insufficient_data", "reason_code": "too_few_clean_laps",
                "reason": f"{used} clean laps in this stint so far; at least 5 needed", "clean_laps_used": used,
                "degradation_s_per_lap": None, "degradation_ci": None, "predicted_laps": None, "pit_window": None,
                "pit_window_reason": None, "remaining_competitive_laps": None}, None
    deg = g.r.uniform(-0.02, 0.15)
    ci = g.maybe([deg - 0.005, deg + 0.005], 0.3)
    pred = g.r.uniform(85, 110)
    out = {**base, "clean_laps_used": g.r.randint(5, 30), "degradation_s_per_lap": deg, "degradation_ci": ci,
           "predicted_laps": [{"lap": lap + 1, "pred_s": pred, "lo_s": pred - 0.6, "hi_s": pred + 0.6}]}
    if kind == "no_window":
        return {**out, "pit_window": None, "pit_window_reason": "forecast_uncertainty_too_high",
                "remaining_competitive_laps": None}, None
    earliest = lap + g.r.randint(1, 5)
    central = g.maybe(g.r.randint(earliest, earliest + 5), 0.3)
    latest = None if central is None else g.maybe(g.r.randint(central, central + 4), 0.4)
    window = {"earliest_lap": earliest, "central_lap": central, "latest_lap": latest, "threshold_s": 1.0,
              "threshold_pace_s": 91.0, "best_pace_s": 90.0, "pace_basis": "fuel_adjusted_model_term",
              "method": "interval_crossing_v2", "confidence": g.choice(["high", "medium", "low"]),
              "confidence_reasons": ["11 clean laps in the stint so far"],
              "assumptions": ["ignores undercut and overcut", "ignores traffic", "ignores track position",
                              "ignores safety-car probability", "ignores tyre availability (sets left)",
                              "threshold is a heuristic (best pace + X seconds), not a team rule"], "notes": []}
    return {**out, "pit_window": window, "pit_window_reason": None,
            "remaining_competitive_laps": None if central is None else central - lap}, None


def build_random_factsheet(seed: int, n_corners: int, n_straights: int, tire_kind: str, drivers: tuple[str, str],
                           session_code: str) -> FactSheet:
    g = _Gen(seed)
    target, reference = drivers
    sid = f"2024-99-R:{target}-vs-{reference}"
    corners = [_corner(g, k, 300.0 * k, sid) for k in range(1, n_corners + 1)]
    straights = [_straight(g, k, 6000.0 + 1000 * k, sid) for k in range(1, n_straights + 1)]
    for i, c in enumerate(sorted(corners, key=lambda c: (-abs(c["time_loss_s"]), c["segment"]["start_m"])), start=1):
        c["rank"] = i
    total = sum(c["time_loss_s"] for c in corners)
    losses = [c["id"] for c in sorted(corners, key=lambda c: -c["time_loss_s"]) if c["time_loss_s"] >= T.noise_floor_s][:3]
    gains = [c["id"] for c in sorted(corners, key=lambda c: c["time_loss_s"]) if c["time_loss_s"] <= -T.noise_floor_s][:3]
    lap = g.r.randint(2, 60)
    t_lap = g.r.uniform(80, 110)

    def ctx(code: str, lap_time: float) -> dict[str, Any]:
        return {"driver": code, "lap_number": lap, "lap_time_s": lap_time, "sector_s": [lap_time / 3] * 3,
                "compound": g.maybe(g.choice(["SOFT", "MEDIUM", "HARD"])), "tyre_age_laps": g.maybe(g.r.randint(1, 30)),
                "fresh_tyre": g.maybe(g.r.random() < 0.5), "air_temp_c": g.mfloat(10, 35), "track_temp_c": g.mfloat(15, 55),
                "rainfall": False, "track_status": "1", "track_status_flags": ["green"]}

    sectors = [{"id": f"{sid}:SEC{k}", "kind": "sector", "label": f"Sector {k}",
                "segment": {"start_m": 2000.0 * (k - 1), "end_m": 2000.0 * k, "apex_m": None},
                "time_loss_s": g.num(-0.4, 0.4), "entry_loss_s": None, "exit_loss_s": None,
                "metrics": {"official_sector_delta_s": g.mnum(-0.4, 0.4), "boundary_discrepancy_s": g.mnum(-0.05, 0.05)},
                "target": {}, "reference": {}, "causes": [], "rank": None, "confidence": "high", "notes": [],
                "corner_number": None, "corner_type": None, "apex_source": None} for k in (1, 2, 3)]
    findings = {
        "schema_version": 1, "comparison_id": sid,
        "session_context": {"session_key": "2024_99_R", "event_name": "RaceIQ Synthetic Grand Prix", "session_name": "Race",
                            "session_code": session_code, "reference": ctx(reference, t_lap - total),
                            "target": ctx(target, t_lap), "notes": ["traffic data unavailable: do not attribute time loss to traffic"]},
        "lap_summary": {"id": f"{sid}:LAP", "kind": "lap_summary", "label": "Lap",
                        "segment": {"start_m": 0.0, "end_m": 7000.0, "apex_m": None}, "time_loss_s": total,
                        "entry_loss_s": None, "exit_loss_s": None,
                        "metrics": {"total_delta_s": total, "official_gap_s": total, "sector_1_delta_s": g.mnum(-0.4, 0.4),
                                    "sector_2_delta_s": g.mnum(-0.4, 0.4), "sector_3_delta_s": g.mnum(-0.4, 0.4),
                                    "top_speed_diff_kph": g.mnum(-10, 10), "corner_count": n_corners,
                                    "corners_lost": sum(c["time_loss_s"] >= T.noise_floor_s for c in corners),
                                    "corners_gained": sum(c["time_loss_s"] <= -T.noise_floor_s for c in corners),
                                    "corners_unclear": sum("unclear" in c["causes"] for c in corners)},
                        "target": {"lap_time_s": t_lap, "top_speed_kph": 330.0},
                        "reference": {"lap_time_s": t_lap - total, "top_speed_kph": 331.0},
                        "causes": [], "rank": None, "confidence": "high", "notes": [], "corner_number": None,
                        "corner_type": None, "apex_source": None},
        "corners": corners, "straights": straights, "sectors": sectors, "largest_losses": losses, "largest_gains": gains,
        "quality": {"sample_count_ref": 800, "sample_count_target": 800, "length_scale_factor": 0.999,
                    "final_delta_error_s": 0.0, "repairs": 0, "dropped_glitches": 0, "tiling_error_s": 0.0,
                    "sector_check_ok": g.r.random() < 0.5, "sector_check_max_discrepancy_s": g.mfloat(0.0, 0.2),
                    "alignment": g.choice(["xy", "distance"]), "xy_correction_rms_m": g.mfloat(0, 8)},
        "notes": ["causes are rules whose thresholds were met (contributing factors), not proof of cause"],
    }
    tire_json, reason = _tire(g, tire_kind, target, lap)
    return build_factsheet(FindingsReport.model_validate(findings), tire_json,
                           FactSheetContext(session_date="2024-12-31", teams={target: TEAMS[target], reference: TEAMS[reference]},
                                            tire_unavailable_reason=reason))


@st.composite
def factsheets(draw: st.DrawFn) -> FactSheet:
    drivers = draw(st.lists(st.sampled_from(DRIVERS), min_size=2, max_size=2, unique=True))
    return build_random_factsheet(
        seed=draw(st.integers(0, 2**32 - 1)), n_corners=draw(st.integers(1, 19)), n_straights=draw(st.integers(0, 4)),
        tire_kind=draw(st.sampled_from(["none", "insufficient", "window", "no_window"])),
        drivers=(drivers[0], drivers[1]), session_code=draw(st.sampled_from(["R", "Q"])))
