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

    lap_55, car_55 = build_lap(driver_number=55, pace_offset_s=+0.15, seed=55)
    lap_4, car_4 = build_lap(driver_number=4, pace_offset_s=0.0, seed=4)

    (HERE / "sessions.json").write_text(json.dumps(sessions, indent=2))
    (HERE / "drivers.json").write_text(json.dumps(drivers, indent=2))
    (HERE / "laps.json").write_text(json.dumps([lap_55, lap_4], indent=2))
    (HERE / "car_data_55.json").write_text(json.dumps(car_55, indent=2))
    (HERE / "car_data_4.json").write_text(json.dumps(car_4, indent=2))

    print(f"driver 55: lap_duration={lap_55['lap_duration']}s, {len(car_55)} car_data rows")
    print(f"driver  4: lap_duration={lap_4['lap_duration']}s, {len(car_4)} car_data rows")


if __name__ == "__main__":
    main()
