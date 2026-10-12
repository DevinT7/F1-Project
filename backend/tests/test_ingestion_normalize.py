"""Phase 1 — pure normalization: FastF1-shaped frames in, domain models out.

Synthetic frames mirror the dtypes FastF1 3.5.3 actually returns (probed against
2024 R5 Qualifying/Race): timedelta64 times, float64 LapNumber/Stint/TyreLife,
object Deleted (None when race-control messages are not loaded), int64 DRS codes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.ingestion import normalize as N
from app.ingestion.models import WeatherSample

td = pd.Timedelta


# ---------------------------------------------------------------- scalar conversion
@pytest.mark.parametrize(
    "value, expected",
    [
        (td(minutes=1, seconds=33.66), 93.66),
        (np.timedelta64(1500, "ms"), 1.5),
        (timedelta(seconds=2.25), 2.25),
        (12.5, 12.5),
        (7, 7.0),
    ],
)
def test_to_seconds_converts_every_timedelta_flavour(value, expected):
    assert N.to_seconds(value) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("value", [None, pd.NaT, np.timedelta64("NaT"), float("nan"), np.nan, "x"])
def test_to_seconds_maps_missing_to_none(value):
    assert N.to_seconds(value) is None


def test_to_seconds_keeps_millisecond_precision():
    assert N.to_seconds(td("0 days 00:01:33.660000")) == 93.66


# ---------------------------------------------------------------- DRS
# Every code observed across all 20 drivers' car data in 2024 R5 Qualifying.
# FastF1 docs: "0-14 (Odd DRS is Disabled, Even DRS is Enabled?)". Community
# decoding: 0/1 off, 8 eligible-not-open, 10/12/14 flap open. See docs/decisions.md.
@pytest.mark.parametrize(
    "code, is_open",
    [(0, False), (1, False), (2, False), (3, False), (8, False), (9, False),
     (10, True), (11, False), (12, True), (14, True)],
)
def test_drs_open_codes(code, is_open):
    assert N.normalize_drs(code) is is_open


@pytest.mark.parametrize("code", [None, float("nan"), -1, 99])
def test_drs_unknown_codes_are_closed(code):
    assert N.normalize_drs(code) is False


# ---------------------------------------------------------------- validity rules
GOOD = dict(lap_time_s=93.7, is_accurate=True, deleted=False,
            n_samples=700, distance_m=5400.0, median_distance_m=5400.0)


def test_valid_lap_has_no_reason():
    assert N.lap_invalid_reason(**GOOD) is None


@pytest.mark.parametrize(
    "override, reason",
    [
        (dict(lap_time_s=None), "no_lap_time"),
        (dict(is_accurate=False), "not_accurate"),
        (dict(deleted=True), "deleted"),
        (dict(n_samples=0, distance_m=None), "no_telemetry"),
        (dict(n_samples=199), "too_few_samples"),
        (dict(distance_m=5400.0 * 1.031), "distance_out_of_range"),
        (dict(distance_m=5400.0 * 0.969), "distance_out_of_range"),
        (dict(median_distance_m=None), "distance_out_of_range"),
    ],
)
def test_each_invalid_reason(override, reason):
    assert N.lap_invalid_reason(**{**GOOD, **override}) == reason


def test_distance_band_is_inclusive_at_three_percent():
    assert N.lap_invalid_reason(**{**GOOD, "distance_m": 5400.0 * 1.0299}) is None
    assert N.lap_invalid_reason(**{**GOOD, "n_samples": 200}) is None


def test_unknown_deleted_does_not_invalidate():
    # deleted=None means FastF1 could not tell (no race control data) — not a deletion.
    assert N.lap_invalid_reason(**{**GOOD, "deleted": None}) is None


# ---------------------------------------------------------------- laps
def _laps_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "Time": [td(seconds=200), td(seconds=300), td(seconds=400)],
        "Driver": ["VER", "VER", "NOR"],
        "DriverNumber": ["1", "1", "4"],
        "LapTime": [td(seconds=93.66), pd.NaT, td(seconds=94.165)],
        "LapNumber": [2.0, 3.0, np.nan],
        "Stint": [1.0, 1.0, np.nan],
        "PitOutTime": [pd.NaT, pd.NaT, td(seconds=250)],
        "PitInTime": [pd.NaT, td(seconds=299), pd.NaT],
        "Sector1Time": [td(seconds=24.1), pd.NaT, td(seconds=24.3)],
        "Sector2Time": [td(seconds=40.0), td(seconds=41), td(seconds=40.2)],
        "Sector3Time": [td(seconds=29.56), pd.NaT, td(seconds=29.665)],
        "Compound": ["SOFT", "SOFT", None],
        "TyreLife": [3.0, 4.0, np.nan],
        "FreshTyre": [True, True, False],
        "Team": ["Red Bull Racing", "Red Bull Racing", "McLaren"],
        "LapStartTime": [td(seconds=106.3), td(seconds=200), td(seconds=305)],
        "TrackStatus": ["1", "12", "1"],
        "Position": [np.nan, np.nan, np.nan],
        "Deleted": [False, None, True],
        "DeletedReason": ["", None, "TRACK LIMITS AT TURN 14 LAP 4 "],
        "IsAccurate": [True, False, True],
    })


def test_normalize_laps_converts_types_and_nat():
    laps = N.normalize_laps(_laps_frame(), "2024_05_Q")
    # The NOR row has no LapNumber; a lap we cannot address is dropped, not crashed on.
    assert [(lap.driver, lap.lap_number) for lap in laps] == [("VER", 2), ("VER", 3)]
    first, second = laps
    assert first.lap_time_s == 93.66
    assert first.sector_s == (24.1, 40.0, 29.56)
    assert first.stint == 1 and first.tyre_age == 3 and first.fresh_tyre is True
    assert first.lap_start_session_time_s == pytest.approx(106.3)
    assert first.lap_end_session_time_s == 200.0
    assert first.pit_in_time_s is None and first.deleted is False
    assert first.deleted_reason is None            # "" is not a reason
    assert second.lap_time_s is None
    assert second.sector_s == (None, 41.0, None)
    assert second.pit_in_time_s == 299.0
    assert second.deleted is None
    assert second.position is None


def test_normalize_laps_without_telemetry_marks_every_lap_invalid():
    laps = N.normalize_laps(_laps_frame(), "2024_05_Q")
    assert laps[0].invalid_reason == "no_telemetry"
    assert laps[1].invalid_reason == "no_lap_time"
    assert not any(lap.is_valid_for_comparison for lap in laps)


def test_normalize_laps_applies_validity_with_session_median():
    frame = pd.DataFrame({
        **{k: v * 4 for k, v in {
            "Driver": ["VER"], "DriverNumber": ["1"], "Team": ["RBR"], "Compound": ["SOFT"],
            "TrackStatus": ["1"], "Deleted": [False], "DeletedReason": [""],
            "IsAccurate": [True], "FreshTyre": [True],
        }.items()},
        "LapNumber": [1.0, 2.0, 3.0, 4.0],
        "LapTime": [td(seconds=s) for s in (95, 94, 93.5, 96)],
        "LapStartTime": [td(seconds=s) for s in (0, 95, 189, 283)],
        "Time": [td(seconds=s) for s in (95, 189, 283, 379)],
    })
    stats = {("VER", 1): (700, 5400.0), ("VER", 2): (650, 5390.0),
             ("VER", 3): (150, 5395.0), ("VER", 4): (700, 5000.0)}
    laps = N.normalize_laps(frame, "k", telemetry_stats=stats)
    assert [lap.invalid_reason for lap in laps] == [None, None, "too_few_samples", "distance_out_of_range"]
    assert [lap.is_valid_for_comparison for lap in laps] == [True, True, False, False]
    assert laps[0].telemetry_samples == 700 and laps[0].telemetry_distance_m == 5400.0


def test_normalize_laps_attaches_nearest_weather_to_lap_start():
    weather = [
        WeatherSample(time_s=60, air_temp_c=22.0, track_temp_c=35.0, humidity=57, rainfall=False, wind_kph=6.0),
        WeatherSample(time_s=120, air_temp_c=23.0, track_temp_c=36.0, humidity=57, rainfall=True, wind_kph=6.0),
        WeatherSample(time_s=180, air_temp_c=24.0, track_temp_c=37.0, humidity=57, rainfall=False, wind_kph=6.0),
    ]
    laps = N.normalize_laps(_laps_frame(), "k", weather=weather)
    assert (laps[0].air_temp_c, laps[0].track_temp_c, laps[0].rainfall) == (23.0, 36.0, True)  # 106.3 → 120
    assert laps[1].air_temp_c == 24.0                                                          # 200 → 180


def test_nearest_weather_edges():
    samples = [WeatherSample(time_s=t, air_temp_c=t, track_temp_c=None, humidity=None,
                             rainfall=None, wind_kph=None) for t in (10.0, 20.0)]
    assert N.nearest_weather(samples, 0.0).time_s == 10.0
    assert N.nearest_weather(samples, 15.0).time_s == 10.0     # tie → earlier sample
    assert N.nearest_weather(samples, 99.0).time_s == 20.0
    assert N.nearest_weather(samples, None) is None
    assert N.nearest_weather([], 5.0) is None


def test_normalize_laps_empty_frame():
    assert N.normalize_laps(pd.DataFrame(), "k") == []


# ---------------------------------------------------------------- telemetry
def _telemetry_frame(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame({
        "Time": [td(milliseconds=100 * i) for i in range(n)],
        "SessionTime": [td(seconds=1000) + td(milliseconds=100 * i) for i in range(n)],
        "Distance": [float(i * 8) for i in range(n)],
        "Speed": [280.0 + i for i in range(n)],
        "Throttle": [100.0] * n,
        "Brake": [False] * (n - 1) + [True],
        "nGear": [7] * n,
        "RPM": [11000.0] * n,
        "DRS": [8, 10, 12, 14, 1][:n],
        "X": [float(i) for i in range(n)],
        "Y": [float(-i) for i in range(n)],
        "Z": [0.0] * n,
        "Source": ["car"] * n,
    })


def test_normalize_telemetry_columns_and_types():
    frame = N.normalize_telemetry(_telemetry_frame())
    assert len(frame) == 5
    np.testing.assert_allclose(frame.time_s, [0.0, 0.1, 0.2, 0.3, 0.4])
    np.testing.assert_allclose(frame.distance_m, [0, 8, 16, 24, 32])
    assert frame.brake.dtype == np.bool_ and frame.brake.tolist() == [False] * 4 + [True]
    assert frame.drs_code.tolist() == [8, 10, 12, 14, 1]
    assert frame.drs_open.tolist() == [False, True, True, True, False]
    assert frame.gear.dtype == np.int64
    assert frame.total_distance_m == 32.0


def test_normalize_telemetry_integrates_distance_when_missing():
    raw = _telemetry_frame().drop(columns=["Distance"])
    raw["Speed"] = 360.0  # 100 m/s
    frame = N.normalize_telemetry(raw)
    np.testing.assert_allclose(frame.distance_m, [0, 10, 20, 30, 40])


def test_normalize_telemetry_sorts_and_drops_duplicate_times():
    raw = _telemetry_frame().iloc[[0, 2, 1, 2, 3]].reset_index(drop=True)
    raw.loc[4, "Time"] = pd.NaT
    frame = N.normalize_telemetry(raw)
    np.testing.assert_allclose(frame.time_s, [0.0, 0.1, 0.2])
    np.testing.assert_allclose(frame.distance_m, [0, 8, 16])


def test_normalize_telemetry_brake_numeric_and_missing_channels():
    raw = _telemetry_frame().drop(columns=["X", "Y", "RPM"])
    raw["Brake"] = [0, 100, 0, 0, 100]
    raw["nGear"] = [7, np.nan, 7, 7, 7]
    frame = N.normalize_telemetry(raw)
    assert frame.brake.tolist() == [False, True, False, False, True]
    assert frame.gear.tolist() == [7, 0, 7, 7, 7]
    assert np.isnan(frame.x).all() and np.isnan(frame.rpm).all()


def test_normalize_telemetry_empty():
    assert len(N.normalize_telemetry(pd.DataFrame())) == 0


# ---------------------------------------------------------------- weather / corners / drivers
def test_normalize_weather_converts_wind_to_kph():
    raw = pd.DataFrame({
        "Time": [td(seconds=57.385)], "AirTemp": [22.1], "Humidity": [57.0], "Pressure": [1007.2],
        "Rainfall": [False], "TrackTemp": [35.5], "WindDirection": [267], "WindSpeed": [1.7],
    })
    [sample] = N.normalize_weather(raw)
    assert sample.time_s == pytest.approx(57.385)
    assert sample.wind_kph == pytest.approx(6.12)   # FastF1 WindSpeed is m/s
    assert (sample.air_temp_c, sample.track_temp_c, sample.rainfall) == (22.1, 35.5, False)


def test_normalize_weather_sorted_and_skips_missing_time():
    raw = pd.DataFrame({"Time": [td(seconds=120), pd.NaT, td(seconds=60)], "AirTemp": [1.0, 2.0, 3.0]})
    assert [s.time_s for s in N.normalize_weather(raw)] == [60.0, 120.0]


def test_normalize_corners():
    raw = pd.DataFrame({
        "X": [-5247.44, -5133.34], "Y": [-2414.48, -986.18], "Number": [1, 2],
        "Letter": ["", "A"], "Angle": [-122.95, -83.2], "Distance": [552.85, 732.28],
    })
    corners = N.normalize_corners(raw)
    assert [(c.number, c.letter) for c in corners] == [(1, None), (2, "A")]
    assert corners[0].distance_m == pytest.approx(552.85)


def test_normalize_drivers():
    raw = pd.DataFrame({
        "DriverNumber": ["1", "4"], "Abbreviation": ["VER", "NOR"],
        "FullName": ["Max Verstappen", "Lando Norris"], "TeamName": ["Red Bull Racing", "McLaren"],
        "TeamColor": ["3671C6", ""], "Position": [1.0, np.nan], "GridPosition": [np.nan, np.nan],
        "Status": ["", "Finished"], "Q1": [td(seconds=94.742), pd.NaT],
        "Q2": [td(seconds=93.794), pd.NaT], "Q3": [td(seconds=93.660), pd.NaT],
    })
    ver, nor = N.normalize_drivers(raw)
    assert ver.code == "VER" and ver.number == "1" and ver.team_color == "#3671C6"
    assert ver.position == 1 and ver.q3_s == 93.66 and ver.status is None
    assert nor.team_color is None and nor.position is None and nor.q1_s is None
    assert nor.status == "Finished"


# ---------------------------------------------------------------- session / schedule / keys
def test_session_key_and_code():
    assert N.make_session_key(2024, 5, "q") == "2024_05_Q"
    assert N.normalize_session_code("Qualifying") == "Q"
    assert N.normalize_session_code("sprint shootout") == "SS"
    assert N.normalize_session_code("fp2") == "FP2"
    with pytest.raises(ValueError):
        N.normalize_session_code("warmup")


def test_session_info_localizes_naive_date_to_utc():
    info = N.normalize_session_info(
        {
            "year": 2024, "round": 5, "event_name": "Chinese Grand Prix", "location": "Shanghai",
            "country": "China", "session_name": "Qualifying", "session_code": "Q",
            "date": pd.Timestamp("2024-04-20 07:00:00"), "total_laps": None,
            "event_format": "sprint_qualifying", "circuit_rotation": 237.0,
        },
    )
    assert info.key == "2024_05_Q"
    assert info.date == datetime(2024, 4, 20, 7, tzinfo=timezone.utc)
    assert info.n_laps_scheduled is None and info.circuit_rotation_deg == 237.0


def test_normalize_schedule():
    raw = pd.DataFrame({
        "RoundNumber": [5], "Country": ["China"], "Location": ["Shanghai"],
        "EventName": ["Chinese Grand Prix"], "EventFormat": ["sprint_qualifying"],
        "EventDate": [pd.Timestamp("2024-04-21")],
        "Session1": ["Practice 1"], "Session1DateUtc": [pd.Timestamp("2024-04-19 03:30")],
        "Session2": ["Sprint Qualifying"], "Session2DateUtc": [pd.Timestamp("2024-04-19 07:30")],
        "Session3": ["Sprint"], "Session3DateUtc": [pd.Timestamp("2024-04-20 03:00")],
        "Session4": ["Qualifying"], "Session4DateUtc": [pd.Timestamp("2024-04-20 07:00")],
        "Session5": ["Race"], "Session5DateUtc": [pd.Timestamp("2024-04-21 07:00")],
    })
    [event] = N.normalize_schedule(raw, 2024)
    assert event.round == 5 and event.event_format == "sprint_qualifying"
    assert [s.code for s in event.sessions] == ["FP1", "SQ", "S", "Q", "R"]
    assert event.sessions[-1].date == datetime(2024, 4, 21, 7, tzinfo=timezone.utc)


def test_raw_records_are_json_safe():
    raw = pd.DataFrame({"Number": [np.int64(1)], "Letter": [""], "Angle": [np.float64(1.5)], "X": [np.nan]})
    assert N.raw_records(raw) == [{"Number": 1, "Letter": "", "Angle": 1.5, "X": None}]
