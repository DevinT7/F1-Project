"""Synthetic builders shared by the Phase 1 ingestion tests (no network, no FastF1)."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.ingestion.fastf1_client import RawSession
from app.ingestion.models import (
    CornerInfo,
    DriverInfo,
    LapRecord,
    SessionBundle,
    SessionInfo,
    TelemetryFrame,
    WeatherSample,
)

KEY = "2024_05_Q"
td = pd.Timedelta


def telemetry(n: int = 300, lap_m: float = 5400.0) -> TelemetryFrame:
    return TelemetryFrame.from_columns({
        "time_s": np.linspace(0, 93.0, n),
        "distance_m": np.linspace(0, lap_m, n),
        "speed_kph": np.full(n, 210.0),
        "throttle_pct": np.full(n, 100.0),
        "brake": np.zeros(n, dtype=bool),
        "gear": np.full(n, 7),
        "rpm": np.full(n, 11000.0),
        "drs_code": np.full(n, 12),
        "drs_open": np.ones(n, dtype=bool),
        "x": np.zeros(n),
        "y": np.zeros(n),
    })


def lap(driver: str = "VER", number: int = 1, time_s: float | None = 93.66, valid: bool = True) -> LapRecord:
    return LapRecord(
        session_key=KEY, driver=driver, driver_number="1" if driver == "VER" else "4", team="T",
        lap_number=number, lap_time_s=time_s, sector_s=(24.0, None, 30.0), stint=1, compound="SOFT",
        tyre_age=2, fresh_tyre=True, pit_in_time_s=None, pit_out_time_s=None, track_status="1",
        is_accurate=True, deleted=False, deleted_reason=None, position=None,
        lap_start_session_time_s=100.0, lap_end_session_time_s=193.66,
        air_temp_c=22.0, track_temp_c=35.5, rainfall=False,
        telemetry_samples=300, telemetry_distance_m=5400.0,
        is_valid_for_comparison=valid, invalid_reason=None if valid else "not_accurate",
    )


def bundle(key: str = KEY) -> SessionBundle:
    year, rnd, code = key.split("_")
    info = SessionInfo(
        key=key, year=int(year), round=int(rnd), event_name="Chinese Grand Prix", circuit="Shanghai",
        country="China", session_code=code, session_name="Qualifying",
        date=datetime(2024, 4, 20, 7, tzinfo=timezone.utc), n_laps_scheduled=None,
        event_format="sprint_qualifying", circuit_rotation_deg=237.0,
    )
    laps = [lap("VER", 1, 95.0), lap("VER", 2, 93.66), lap("VER", 3, 92.0, valid=False),
            lap("NOR", 1, 94.165), lap("NOR", 2, None, valid=False)]
    return SessionBundle(
        info=info,
        drivers=[DriverInfo(code="VER", number="1", full_name="Max Verstappen", team="Red Bull Racing",
                            team_color="#3671C6", position=1, q3_s=93.66),
                 DriverInfo(code="NOR", number="4", full_name="Lando Norris", team="McLaren",
                            team_color="#FF8000", position=4)],
        laps=laps,
        weather=[WeatherSample(time_s=57.4, air_temp_c=22.1, track_temp_c=35.5, humidity=57.0,
                               rainfall=False, wind_kph=6.12)],
        corners=[CornerInfo(number=1, letter=None, distance_m=552.9, x=-5247.4, y=-2414.5, angle=-123.0)],
        telemetry={("VER", 1): telemetry(), ("VER", 2): telemetry(310), ("NOR", 1): telemetry(305)},
        corners_raw=[{"Number": 1, "Letter": "", "Distance": 552.9}],
        fastf1_version="3.5.3",
    )


def raw_session(year: int = 2024, rnd: int = 5, code: str = "Q") -> RawSession:
    laps = pd.DataFrame({
        "Driver": ["VER", "VER", "NOR"],
        "DriverNumber": ["1", "1", "4"],
        "Team": ["Red Bull Racing", "Red Bull Racing", "McLaren"],
        "LapNumber": [1.0, 2.0, 1.0],
        "LapTime": [td(seconds=95), td(seconds=93.66), td(seconds=94.165)],
        "LapStartTime": [td(seconds=100), td(seconds=195), td(seconds=101)],
        "Time": [td(seconds=195), td(seconds=288.66), td(seconds=195.165)],
        "IsAccurate": [True, True, True],
        "Deleted": [False, False, False],
        "Compound": ["SOFT"] * 3,
    })
    return RawSession(
        year=year, round=rnd, session_code=code,
        info={"year": year, "round": rnd, "event_name": "Chinese Grand Prix", "location": "Shanghai",
              "country": "China", "event_format": "sprint_qualifying", "session_name": "Qualifying",
              "session_code": code, "date": pd.Timestamp("2024-04-20 07:00"), "total_laps": None,
              "circuit_rotation": 237.0},
        laps=laps,
        results=pd.DataFrame({"Abbreviation": ["VER", "NOR"], "DriverNumber": ["1", "4"],
                              "TeamColor": ["3671C6", "FF8000"]}),
        weather=pd.DataFrame({"Time": [td(seconds=60)], "AirTemp": [22.0], "WindSpeed": [1.0]}),
        corners=pd.DataFrame({"Number": [1], "Letter": [""], "Distance": [552.9], "X": [0.0],
                              "Y": [0.0], "Angle": [10.0]}),
        fastf1_version="3.5.3",
    )


def raw_telemetry_frame(n: int = 400, lap_m: float = 5400.0) -> pd.DataFrame:
    return pd.DataFrame({
        "Time": pd.to_timedelta(np.linspace(0, 93, n), unit="s"),
        "Distance": np.linspace(0, lap_m, n),
        "Speed": np.full(n, 210.0),
        "Throttle": np.full(n, 100.0),
        "Brake": np.zeros(n, dtype=bool),
        "nGear": np.full(n, 7),
        "RPM": np.full(n, 11000.0),
        "DRS": np.full(n, 8),
        "X": np.zeros(n),
        "Y": np.zeros(n),
    })
