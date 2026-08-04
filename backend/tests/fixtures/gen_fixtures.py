"""
Generate offline OpenF1-shaped fixtures for session_key=9159 (2023 Singapore GP).

No network access is available in this environment, so this builds a synthetic
but schema-accurate reconstruction: same field names/types as the real OpenF1
`sessions` / `drivers` / `laps` / `car_data` endpoints, with a plausible
Marina Bay-style speed profile (19 corners, low top speed, heavy braking).

Run: python3 gen_fixtures.py   (writes the *.json files in this directory)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
SESSION_KEY = 9159
LAP_NUMBER = 18
LAP_START = datetime(2023, 9, 17, 12, 30, 0, tzinfo=timezone.utc)

# Approximate corner distances (m) around a ~5063 m Marina Bay-style lap.
CORNERS = [120, 340, 520, 780, 1040, 1260, 1480, 1700, 1980, 2260,
           2520, 2760, 3020, 3300, 3560, 3820, 4080, 4420, 4780]
LAP_LENGTH_M = 5063.0


def _fmt(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def build_lap(driver_number: int, pace_offset_s: float, seed: int,
             track_seed: int = 1) -> dict:
    """Return (lap_row, car_data_rows) for one flying lap.

    `track_seed` is shared across drivers so both laps trace the *same* circuit
    (same corner depths/widths); `seed` only drives this driver's small sample
    noise so the comparison is apples-to-apples.
    """
    track_rng = np.random.default_rng(track_seed)
    rng = np.random.default_rng(seed)

    # ---- synthesize a distance/speed profile, then convert to a time series ----
    d = np.arange(0.0, LAP_LENGTH_M, 2.0)
    speed = np.full_like(d, 295.0)  # long-straight top speed (km/h), race trim
    for c in CORNERS:
        depth = track_rng.uniform(70, 190)      # how hard the braking zone is
        width = track_rng.uniform(45, 90)
        speed -= depth * np.exp(-((d - c) ** 2) / (2 * width ** 2))
    speed = np.clip(speed + rng.normal(0, 1.5, size=d.shape), 60, 300)
    # small constant pace offset between drivers (km/h bias -> faster/slower overall)
    speed = np.clip(speed + pace_offset_s, 55, 300)

    # Saturates at 100 once speed is within ~90% of top speed, like a real full-
    # throttle straight, instead of asymptotically approaching ~98% and never
    # quite tipping the analysis engine's throttle_delay full-throttle threshold.
    throttle = np.clip((speed - 150.0) / 1.2, 0, 100)
    brake = np.where(np.diff(speed, prepend=speed[0]) < -0.8, 100.0, 0.0)
    n_gear = np.clip((speed // 35).astype(int) + 1, 1, 8)
    rpm = 4000 + (speed / 300.0) * 8000
    drs = np.where((speed > 260) & (throttle > 90), 1, 0)

    # integrate distance -> time using this same speed profile (m/s), so the
    # resulting car_data is naturally time-sampled like the real endpoint.
    v_ms = np.clip(speed * (1000.0 / 3600.0), 5.0, None)
    dt_seg = np.diff(d, prepend=d[0]) / v_ms
    t_cum = np.cumsum(dt_seg)
    lap_duration = float(t_cum[-1])

    # resample onto a uniform ~0.2s wall-clock grid (OpenF1 car_data cadence)
    t_grid = np.arange(0.0, lap_duration, 0.2)
    speed_t = np.interp(t_grid, t_cum, speed)
    throttle_t = np.interp(t_grid, t_cum, throttle)
    brake_t = np.interp(t_grid, t_cum, brake)
    n_gear_t = np.interp(t_grid, t_cum, n_gear).round().astype(int)
    rpm_t = np.interp(t_grid, t_cum, rpm)
    drs_t = np.interp(t_grid, t_cum, drs).round().astype(int)

    car_data = []
    for i, t in enumerate(t_grid):
        ts = LAP_START + timedelta(seconds=float(t))
        car_data.append({
            "session_key": SESSION_KEY,
            "driver_number": driver_number,
            "date": _fmt(ts),
            "speed": round(float(speed_t[i]), 1),
            "throttle": round(float(throttle_t[i]), 1),
            "brake": round(float(brake_t[i]), 1),
            "n_gear": int(n_gear_t[i]),
            "rpm": round(float(rpm_t[i]), 0),
            "drs": int(drs_t[i]),
        })

    # rough even-thirds sector split of the lap time
    s1 = lap_duration * 0.33 + rng.uniform(-0.2, 0.2)
    s2 = lap_duration * 0.34 + rng.uniform(-0.2, 0.2)
    s3 = lap_duration - s1 - s2

    lap_row = {
        "session_key": SESSION_KEY,
        "driver_number": driver_number,
        "lap_number": LAP_NUMBER,
        "date_start": _fmt(LAP_START),
        "lap_duration": round(lap_duration, 3),
        "duration_sector_1": round(s1, 3),
        "duration_sector_2": round(s2, 3),
        "duration_sector_3": round(s3, 3),
        "is_pit_out_lap": False,
        "st_speed": round(float(np.max(speed_t)), 1),
    }
    return lap_row, car_data


# ------------------------------------------------------------------ race stints
# Person 3 (tire degradation) needs a *multi-lap race* fixture, not a single flying
# lap. Driver 16 runs a realistic 2-stop-shaped race; driver 81 is a degenerate
# stint (< 3 clean laps) so the `_empty` branch of `analyse_current_stint` is covered.
RACE_START = datetime(2023, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
BASE_LAP_S = 100.0            # clean-air race pace on fresh tyres
FUEL_EFFECT_S_PER_LAP = 0.03  # car gets this much faster per lap as fuel burns off
SC_LAP = 35                   # safety-car-inflated lap (driver 16)
MISSING_LAP = 40              # lap with no lap_duration (driver 16)


def _race_lap(driver_number: int, lap_number: int, duration: float | None,
              is_pit_out: bool) -> dict:
    return {
        "session_key": SESSION_KEY,
        "driver_number": driver_number,
        "lap_number": lap_number,
        "date_start": _fmt(RACE_START + timedelta(seconds=BASE_LAP_S * (lap_number - 1))),
        "lap_duration": None if duration is None else round(duration, 3),
        "duration_sector_1": None if duration is None else round(duration * 0.33, 3),
        "duration_sector_2": None if duration is None else round(duration * 0.34, 3),
        "duration_sector_3": None if duration is None else round(duration * 0.33, 3),
        "is_pit_out_lap": is_pit_out,
        "st_speed": 290.0,
    }


def build_race(driver_number: int, plan: list[dict], seed: int) -> tuple[list, list, list]:
    """Build (stint_rows, lap_rows, pit_rows) for one driver from a stint `plan`.

    Each plan entry: {compound, lap_start, lap_end, age0, deg} where `deg` is the
    true tyre degradation in s/lap. Observed lap time = base + deg*age - fuel*lap,
    so a consumer that doesn't fuel-correct sees `deg - FUEL_EFFECT_S_PER_LAP`.
    """
    rng = np.random.default_rng(seed)
    stints, laps, pits = [], [], []
    for n, s in enumerate(plan, start=1):
        stints.append({
            "session_key": SESSION_KEY, "meeting_key": 1219,
            "driver_number": driver_number, "stint_number": n,
            "lap_start": s["lap_start"], "lap_end": s["lap_end"],
            "compound": s["compound"], "tyre_age_at_start": s["age0"],
        })
        for lap in range(s["lap_start"], s["lap_end"] + 1):
            age = s["age0"] + (lap - s["lap_start"])
            t = (BASE_LAP_S + s["deg"] * age - FUEL_EFFECT_S_PER_LAP * (lap - 1)
                 + float(rng.normal(0, 0.12)))
            is_pit_out = lap == s["lap_start"] and n > 1
            if is_pit_out:
                t += 2.5                      # cold tyres out of the pit box
            if lap == SC_LAP:
                t += 18.0                     # safety car
            if lap == MISSING_LAP:
                t = None
            laps.append(_race_lap(driver_number, lap, t, is_pit_out))
        if n < len(plan):                     # pitted at the end of this stint
            pits.append({
                "session_key": SESSION_KEY, "meeting_key": 1219,
                "driver_number": driver_number, "lap_number": s["lap_end"],
                "date": _fmt(RACE_START + timedelta(seconds=BASE_LAP_S * s["lap_end"])),
                "pit_duration": 22.4,
            })
    return stints, laps, pits


def build_weather(total_laps: int = 58) -> list[dict]:
    """One row per ~5 laps, track temp falling through a night race."""
    rows = []
    for i, lap in enumerate(range(1, total_laps + 1, 5)):
        rows.append({
            "session_key": SESSION_KEY, "meeting_key": 1219,
            "date": _fmt(RACE_START + timedelta(seconds=BASE_LAP_S * (lap - 1))),
            "air_temperature": round(30.5 - 0.2 * i, 1),
            "track_temperature": round(34.0 - 0.5 * i, 1),
            "humidity": 72.0, "pressure": 1008.5, "rainfall": 0,
            "wind_direction": 210, "wind_speed": 1.4,
        })
    return rows


def main() -> None:
    sessions = [{
        "session_key": SESSION_KEY,
        "meeting_key": 1219,
        "year": 2023,
        "country_name": "Singapore",
        "circuit_short_name": "Marina Bay",
        "session_name": "Race",
        "session_type": "Race",
        "date_start": _fmt(LAP_START - timedelta(minutes=90)),
    }]

    drivers = [
        {"session_key": SESSION_KEY, "driver_number": 55, "full_name": "Carlos Sainz",
         "name_acronym": "SAI", "team_name": "Ferrari", "team_colour": "E8002D",
         "headshot_url": None},
        {"session_key": SESSION_KEY, "driver_number": 4, "full_name": "Lando Norris",
         "name_acronym": "NOR", "team_name": "McLaren", "team_colour": "FF8000",
         "headshot_url": None},
    ]

    drivers += [
        {"session_key": SESSION_KEY, "driver_number": 16, "full_name": "Charles Leclerc",
         "name_acronym": "LEC", "team_name": "Ferrari", "team_colour": "E8002D",
         "headshot_url": None},
        {"session_key": SESSION_KEY, "driver_number": 81, "full_name": "Oscar Piastri",
         "name_acronym": "PIA", "team_name": "McLaren", "team_colour": "FF8000",
         "headshot_url": None},
    ]

    lap_55, car_55 = build_lap(driver_number=55, pace_offset_s=+0.15, seed=55)
    lap_4, car_4 = build_lap(driver_number=4, pace_offset_s=0.0, seed=4)

    # 16: normal 2-stint race (MEDIUM 1-22, HARD 23-58) — pit lap, SC lap, missing lap
    stints_16, laps_16, pits_16 = build_race(16, [
        {"compound": "MEDIUM", "lap_start": 1, "lap_end": 22, "age0": 0, "deg": 0.09},
        {"compound": "HARD", "lap_start": 23, "lap_end": 58, "age0": 0, "deg": 0.06},
    ], seed=16)
    # 81: degenerate — 3 laps, one of them a pit-out => < 3 clean laps
    stints_81, laps_81, pits_81 = build_race(81, [
        {"compound": "SOFT", "lap_start": 1, "lap_end": 2, "age0": 0, "deg": 0.15},
        {"compound": "SOFT", "lap_start": 3, "lap_end": 4, "age0": 0, "deg": 0.15},
    ], seed=81)

    (HERE / "sessions.json").write_text(json.dumps(sessions, indent=2))
    (HERE / "drivers.json").write_text(json.dumps(drivers, indent=2))
    (HERE / "laps.json").write_text(
        json.dumps([lap_55, lap_4] + laps_16 + laps_81, indent=2))
    (HERE / "car_data_55.json").write_text(json.dumps(car_55, indent=2))
    (HERE / "car_data_4.json").write_text(json.dumps(car_4, indent=2))
    (HERE / "stints.json").write_text(json.dumps(stints_16 + stints_81, indent=2))
    (HERE / "pit.json").write_text(json.dumps(pits_16 + pits_81, indent=2))
    (HERE / "weather.json").write_text(json.dumps(build_weather(), indent=2))

    print(f"driver 55: lap_duration={lap_55['lap_duration']}s, {len(car_55)} car_data rows")
    print(f"driver  4: lap_duration={lap_4['lap_duration']}s, {len(car_4)} car_data rows")
    print(f"driver 16: {len(stints_16)} stints, {len(laps_16)} race laps, {len(pits_16)} pit")
    print(f"driver 81: {len(stints_81)} stints, {len(laps_81)} race laps (degenerate)")


if __name__ == "__main__":
    main()
