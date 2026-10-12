"""Every tunable number in the corner/insight engine, in one documented place.

Distances are metres, speeds km/h, times seconds. Pass a modified Thresholds to
generate_findings to change behaviour; defaults follow the Phase 3 spec and are
documented in docs/insight_schema.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    # --- apex detection (reference lap) ---
    apex_smooth_m: float = 9.0             # Savitzky–Golay window before finding minima
    apex_prominence_kph: float = 8.0       # minimum prominence of a speed minimum
    apex_match_m: float = 60.0             # official corner ↔ detected minimum window
    xy_tolerance_m: float = 50.0           # official corner X/Y vs lap X/Y at that distance
    xy_units_per_m: float = 10.0           # FastF1 X/Y are decimetres (verified: path/Distance = 9.95)

    # --- corner type by reference minimum speed ---
    slow_below_kph: float = 120.0
    fast_above_kph: float = 220.0

    # --- per-corner metric windows ---
    braking_window_m: float = 400.0        # search [A - 400, A], clipped to the segment
    brake_min_duration_s: float = 0.3      # brake must stay on this long to count
    entry_fallback_m: float = 100.0        # entry speed at A - 100 m when there is no braking
    min_speed_window_m: float = 80.0       # min speed in [A - 80, A + 80]
    exit_offset_m: float = 100.0           # exit speed at A + 100 m
    throttle_pickup_pct: float = 20.0
    full_throttle_pct: float = 98.0
    full_throttle_min_s: float = 0.3

    # --- straights (reference lap) ---
    straight_min_m: float = 300.0
    straight_throttle_pct: float = 95.0

    # --- attribution rules: a cause is reported only when its threshold is met ---
    braking_diff_m: float = 5.0            # |Δ braking start|
    min_speed_diff_kph: float = 3.0        # |Δ minimum speed|
    full_throttle_diff_s: float = 0.15     # |Δ time to full throttle|
    exit_speed_diff_kph: float = 3.0       # |Δ exit speed|
    top_speed_diff_kph: float = 3.0        # |Δ top speed| on straights

    # --- confidence / ranking ---
    noise_floor_s: float = 0.02            # |time loss| below this → confidence "low", never ranked
    min_samples_per_km: float = 60.0       # raw telemetry density below this → "low"
    top_n: int = 3
