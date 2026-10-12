"""Phase 1 — offline checks against the trimmed REAL fixture (2024 R5 Qualifying).

tests/fixtures/ingestion/ holds 3 drivers x 3 laps captured from FastF1 3.5.3; see
gen_fixture.py. raw/ is adapter output, store/ is the ParquetRepository layout with
validity computed against the full 279-lap session.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ingestion import normalize as N
from app.ingestion.models import TELEMETRY_COLUMNS
from app.ingestion.service import get_fastest_valid_lap
from app.storage.repository import ParquetRepository

FIXTURE = Path(__file__).parent / "fixtures" / "ingestion"
RAW = FIXTURE / "raw"
KEY = "2024_05_Q"
DRIVERS = ("VER", "PER", "ALO")


@pytest.fixture(scope="module")
def store():
    return ParquetRepository(FIXTURE / "store")


def test_fixture_is_small():
    size = sum(p.stat().st_size for p in FIXTURE.rglob("*") if p.is_file())
    assert size < 5 * 1024 * 1024


def test_fastest_valid_lap_matches_official_for_three_drivers(store):
    # Official classification = best of Q1/Q2/Q3 from session.results.
    drivers = {d.code: d for d in store.list_drivers(KEY)}
    assert set(drivers) == set(DRIVERS)
    for code in DRIVERS:
        official = min(t for t in (drivers[code].q1_s, drivers[code].q2_s, drivers[code].q3_s) if t)
        assert get_fastest_valid_lap(store, KEY, code).lap_time_s == pytest.approx(official, abs=0.001)


def test_official_times_are_the_known_values(store):
    times = {d.code: d.q3_s for d in store.list_drivers(KEY)}
    assert times == {"VER": 93.66, "PER": 93.982, "ALO": 94.148}


def test_stored_session_metadata(store):
    info = store.get_session_info(KEY)
    assert (info.event_name, info.circuit, info.session_code) == ("Chinese Grand Prix", "Shanghai", "Q")
    assert info.date.isoformat() == "2024-04-20T07:00:00+00:00"
    assert info.circuit_rotation_deg == 237.0
    assert len(store.get_corners(KEY)) == 16
    assert len(store.get_weather(KEY)) > 50
    manifest = store.get_manifest(KEY)
    assert manifest.fastf1_version == "3.5.3"


def test_stored_validity_outcomes(store):
    laps = store.list_laps(KEY)
    assert len(laps) == 9
    reasons = {lap.invalid_reason for lap in laps}
    assert None in reasons and "no_lap_time" in reasons
    for lap in laps:
        assert lap.is_valid_for_comparison == (lap.invalid_reason is None)
        assert lap.air_temp_c is not None and lap.track_temp_c is not None


def test_stored_telemetry_has_every_channel(store):
    for lap in store.list_laps(KEY):
        tel = store.get_telemetry(KEY, lap.driver, lap.lap_number)
        assert len(tel) >= 200
        assert set(tel.columns()) == set(TELEMETRY_COLUMNS)
        assert np.all(np.diff(tel.time_s) > 0) and np.all(np.diff(tel.distance_m) >= 0)
        assert not np.isnan(tel.speed_kph).any() and not np.isnan(tel.x).any()
        assert 4500 < tel.total_distance_m < 6000


# ---------------------------------------------------------------- normalize on raw frames
def test_raw_laps_have_the_columns_the_plan_needs():
    laps = pd.read_parquet(RAW / "laps.parquet")
    required = {"Driver", "DriverNumber", "Team", "LapNumber", "LapTime", "Sector1Time", "Sector2Time",
                "Sector3Time", "Stint", "Compound", "TyreLife", "FreshTyre", "PitInTime", "PitOutTime",
                "TrackStatus", "IsAccurate", "Deleted", "DeletedReason", "LapStartTime", "Time", "Position"}
    assert required <= set(laps.columns)
    assert laps["Deleted"].notna().all()  # messages=True populated it


def test_normalize_raw_laps_reproduces_stored_lap_fields(store):
    raw = pd.read_parquet(RAW / "laps.parquet")
    normalized = {(l.driver, l.lap_number): l for l in N.normalize_laps(raw, KEY)}
    for stored in store.list_laps(KEY):
        mine = normalized[(stored.driver, stored.lap_number)]
        assert mine.lap_time_s == stored.lap_time_s
        assert mine.sector_s == stored.sector_s
        assert (mine.compound, mine.tyre_age, mine.stint) == (stored.compound, stored.tyre_age, stored.stint)


def test_normalize_raw_telemetry_reproduces_stored_frames(store):
    for path in sorted((RAW / "telemetry").glob("*.parquet")):
        driver, lap = path.stem.split("_")
        mine = N.normalize_telemetry(pd.read_parquet(path))
        stored = store.get_telemetry(KEY, driver, int(lap))
        for name in TELEMETRY_COLUMNS:
            np.testing.assert_array_equal(getattr(mine, name), getattr(stored, name), err_msg=name)


def test_every_real_drs_code_is_handled():
    codes = set()
    for path in (RAW / "telemetry").glob("*.parquet"):
        codes |= set(pd.read_parquet(path)["DRS"].unique().tolist())
    assert codes <= {0, 1, 2, 3, 8, 9, 10, 11, 12, 14}
    assert codes & N.DRS_OPEN_CODES  # a quali lap opens the flap somewhere


def test_normalize_raw_session_info_weather_corners_drivers():
    info = N.normalize_session_info(json.loads((RAW / "info.json").read_text()))
    assert info.key == KEY and info.date.tzinfo is not None
    weather = N.normalize_weather(pd.read_parquet(RAW / "weather.parquet"))
    assert all(w.wind_kph is not None and w.wind_kph < 40 for w in weather)
    corners = N.normalize_corners(pd.read_parquet(RAW / "corners.parquet"))
    assert [c.number for c in corners] == list(range(1, 17))
    drivers = N.normalize_drivers(pd.read_parquet(RAW / "results.parquet"))
    assert {d.code for d in drivers} == set(DRIVERS)
    assert all(d.team_color and d.team_color.startswith("#") for d in drivers)
