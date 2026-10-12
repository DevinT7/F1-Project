"""Synthetic lap generator with a known answer.

A lap is defined by a speed profile v(d) over distance (straights, braking zones,
corners). The true elapsed time is t(d) = ∫ dd / v(d), integrated on a 0.05 m grid, so
every test can compare the alignment engine against the exact curve. Laps can be
sampled at any rate, with timestamp jitter, distance drift (the measured Distance is
scaled by 1 + drift, like FastF1's speed integration), uniform speed scaling, and a
slow zone that concentrates time loss in one corner.

Like FastF1's Lap.get_telemetry(), the first and last samples sit exactly on the lap
start and end, so telemetry duration == lap_time_s.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.ndimage import gaussian_filter1d

from app.ingestion.models import CornerInfo, TelemetryFrame
from app.processing.models import LapData

FINE_STEP_M = 0.05
SMOOTH_M = 30.0  # real speed traces are smooth; kinks would make linear interp look worse than it is

# (distance from previous knot in m, speed at knot in km/h) — a ~5.0 km lap.
KNOTS: tuple[tuple[float, float], ...] = (
    (0.0, 290.0),
    (700.0, 315.0),   # main straight
    (130.0, 95.0),    # T1 braking zone
    (90.0, 90.0),     # T1 apex
    (260.0, 240.0),   # exit / short straight
    (110.0, 140.0),   # T3 braking
    (70.0, 135.0),    # T3 apex
    (420.0, 285.0),
    (500.0, 300.0),   # back straight
    (140.0, 80.0),    # hairpin braking
    (60.0, 75.0),     # hairpin apex   (~2480 m)
    (380.0, 250.0),
    (300.0, 230.0),   # fast sweepers
    (150.0, 200.0),
    (200.0, 260.0),
    (450.0, 305.0),
    (120.0, 120.0),   # chicane braking
    (80.0, 110.0),    # chicane apex
    (330.0, 260.0),
    (330.0, 290.0),   # back to the line (~4820 m)
)
HAIRPIN_ZONE: tuple[float, float] = (2340.0, 2600.0)
XY_PER_M = 10.0

# Knot indices for corner tests. Braking for the hairpin starts at knot 8 (2280 m) and
# reaches 80 km/h at knot 9; T2's apex plateau is around knot 6 (1360 m).
HAIRPIN_BRAKE_KNOTS = (8, 9)
T2_APEX_M = 1345.0

# Official corner table for the synthetic track, like FastF1's circuit_info.corners:
#   T1 ~ 900 m, T2 ~ 1345 m, T3 hairpin ~ 2470 m, T4 fast sweeper ~ 3310 m (flat out).
#   T5 is a decoy on the main straight (no speed minimum: tests the official fallback).
#   The chicane minimum (~4150 m) is deliberately missing (tests the detected fallback).
OFFICIAL_CORNERS: tuple[tuple[int, float], ...] = ((1, 900.0), (2, 1345.0), (3, 2470.0), (4, 3310.0), (5, 350.0))


@dataclass(frozen=True)
class Profile:
    distance_m: np.ndarray   # fine grid, 0..L
    speed_kph: np.ndarray
    time_s: np.ndarray       # exact elapsed time at each fine-grid distance

    @property
    def length_m(self) -> float:
        return float(self.distance_m[-1])

    @property
    def lap_time_s(self) -> float:
        return float(self.time_s[-1])

    def time_at(self, d: np.ndarray | float) -> np.ndarray:
        return np.interp(d, self.distance_m, self.time_s)

    def speed_at(self, d: np.ndarray | float) -> np.ndarray:
        return np.interp(d, self.distance_m, self.speed_kph)


@lru_cache(maxsize=64)
def make_profile(
    *,
    speed_scale: float = 1.0,
    slow_zone: tuple[float, float] | None = None,
    slow_factor: float = 1.0,
    knot_shifts: tuple[tuple[int, float], ...] = (),
    speed_offset: tuple[float, float, float] | None = None,
) -> Profile:
    """knot_shifts moves knot i by shift metres (e.g. a braking zone 18 m earlier).
    speed_offset (centre_m, half_width_m, kph) adds kph on a flat plateau of
    ±half_width with 30 m cosine tapers, applied after smoothing so the change
    at the plateau is exact (e.g. a minimum speed exactly 7 km/h lower)."""
    knot_d = np.cumsum([k[0] for k in KNOTS])
    for index, shift in knot_shifts:
        knot_d[index] += shift
    knot_v = np.array([k[1] for k in KNOTS])
    d = np.arange(0.0, knot_d[-1] + FINE_STEP_M / 2, FINE_STEP_M)
    v = np.interp(d, knot_d, knot_v)
    v = gaussian_filter1d(v, sigma=SMOOTH_M / 2 / FINE_STEP_M, mode="wrap")
    v = v * speed_scale
    if slow_zone is not None:
        lo, hi = slow_zone
        # smooth bump so the slow zone has no step in speed
        bump = np.clip(np.sin(np.pi * (d - lo) / (hi - lo)), 0, None) * ((d >= lo) & (d <= hi))
        v = v * (1.0 - (1.0 - slow_factor) * bump)
    if speed_offset is not None:
        centre, half, kph = speed_offset
        dist = np.abs(d - centre) - half
        taper = np.where(dist <= 0, 1.0, np.where(dist < 30.0, 0.5 * (1 + np.cos(np.pi * dist / 30.0)), 0.0))
        v = v + kph * taper
    v_ms = v / 3.6
    t = np.concatenate(([0.0], np.cumsum(np.diff(d) * 2.0 / (v_ms[1:] + v_ms[:-1]))))
    return Profile(distance_m=d, speed_kph=v, time_s=t)


def sample_lap(
    profile: Profile,
    *,
    hz: float = 10.0,
    jitter_s: float = 0.0,
    drift: float = 0.0,
    seed: int = 0,
    driver: str = "SYN",
    lap_number: int = 1,
    lap_time_s: float | None = None,
    sectors: bool = True,
) -> LapData:
    rng = np.random.default_rng(seed)
    total = profile.lap_time_s
    t = np.arange(0.0, total, 1.0 / hz)
    if jitter_s:
        t[1:] = t[1:] + rng.uniform(-jitter_s, jitter_s, size=len(t) - 1)
        t = np.sort(np.clip(t, 0.0, total * (1 - 1e-9)))
    t = np.concatenate((t, [total]))
    d_true = np.interp(t, profile.time_s, profile.distance_m)
    speed = profile.speed_at(d_true)
    accel = np.gradient(speed, t)
    radius = profile.length_m / (2 * np.pi)
    gear = np.digitize(speed, [80, 120, 160, 195, 230, 260, 290]) + 1
    sector_s = (None, None, None)
    if sectors:
        b1, b2 = profile.length_m / 3, 2 * profile.length_m / 3
        t1, t2 = float(profile.time_at(b1)), float(profile.time_at(b2))
        sector_s = (round(t1, 3), round(t2 - t1, 3), round(total - t2, 3))
    frame = TelemetryFrame.from_columns({
        "time_s": t,
        "distance_m": d_true * (1.0 + drift),
        "speed_kph": speed,
        "throttle_pct": np.where(accel >= 0, 100.0, 0.0),
        "brake": accel < -15.0,
        "gear": gear,
        "rpm": 7000.0 + 18.0 * speed,
        "drs_code": np.where((d_true > 100) & (d_true < 700), 12, 8),
        "drs_open": (d_true > 100) & (d_true < 700),
        # FastF1 X/Y are decimetres: a circle whose circumference is the lap length
        "x": XY_PER_M * radius * np.cos(2 * np.pi * d_true / profile.length_m),
        "y": XY_PER_M * radius * np.sin(2 * np.pi * d_true / profile.length_m),
    })
    return LapData(
        telemetry=frame,
        lap_time_s=total if lap_time_s is None else lap_time_s,
        driver=driver,
        lap_number=lap_number,
        sector_s=sector_s,
    )


def make_lap(**kwargs: object) -> LapData:
    """make_profile + sample_lap in one call; profile kwargs are split out."""
    profile_keys = {"speed_scale", "slow_zone", "slow_factor"}
    profile = make_profile(**{k: v for k, v in kwargs.items() if k in profile_keys})  # type: ignore[arg-type]
    return sample_lap(profile, **{k: v for k, v in kwargs.items() if k not in profile_keys})  # type: ignore[arg-type]


def official_corners(profile: Profile) -> list[CornerInfo]:
    radius = profile.length_m / (2 * np.pi)
    corners = []
    for number, dist in OFFICIAL_CORNERS:
        angle = 2 * np.pi * dist / profile.length_m
        corners.append(CornerInfo(
            number=number, letter=None, distance_m=dist, angle=0.0,
            x=float(XY_PER_M * radius * np.cos(angle)), y=float(XY_PER_M * radius * np.sin(angle)),
        ))
    return sorted(corners, key=lambda c: c.distance_m)
