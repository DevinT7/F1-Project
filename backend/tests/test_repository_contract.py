"""Phase 8 — the SessionRepository contract, run against EVERY implementation (Parquet, Postgres).

Behaviour only: anything a caller of SessionRepository can observe. Implementation details
(the Parquet file layout, atomic file swaps) stay in tests/test_ingestion_repository.py.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import numpy as np
import pytest

from app.ingestion import models
from app.ingestion.errors import SessionNotFoundError, TelemetryUnavailableError
from tests import ingestion_synth as synth
from tests import repository_backends as backends

BACKENDS = ["parquet", "postgres"]


@pytest.fixture(params=BACKENDS)
def repo(request, tmp_path):
    yield from backends.make_repo(request.param, tmp_path)


@pytest.fixture(scope="module", autouse=True)
def _stop_container():
    yield
    backends.stop()


def _same_frame(a: models.TelemetryFrame, b: models.TelemetryFrame) -> None:
    for name in models.TELEMETRY_COLUMNS:
        np.testing.assert_array_equal(getattr(a, name), getattr(b, name), err_msg=name)
        assert getattr(a, name).dtype == getattr(b, name).dtype, name


# ---------------------------------------------------------------- round trip
def test_round_trip(repo):
    bundle = synth.bundle()
    repo.save_session(bundle)
    assert repo.has_session(synth.KEY)
    assert repo.get_session_info(synth.KEY) == bundle.info
    assert repo.get_session_info(synth.KEY).date.tzinfo is not None
    assert repo.list_drivers(synth.KEY) == bundle.drivers
    assert repo.list_laps(synth.KEY) == bundle.laps
    assert repo.get_corners(synth.KEY) == bundle.corners
    assert repo.get_weather(synth.KEY) == bundle.weather
    assert repo.get_corners_raw(synth.KEY) == bundle.corners_raw
    for (driver, lap), frame in bundle.telemetry.items():
        _same_frame(repo.get_telemetry(synth.KEY, driver, lap), frame)


def test_manifest(repo):
    assert repo.get_manifest(synth.KEY) is None
    repo.save_session(synth.bundle())
    m = repo.get_manifest(synth.KEY)
    assert m.session_key == synth.KEY and m.schema_version == models.SCHEMA_VERSION
    assert (m.lap_count, m.telemetry_count, m.driver_count) == (5, 3, 2)
    assert m.fastf1_version == "3.5.3" and m.ingested_at.tzinfo is not None


def test_telemetry_keeps_nan_and_exact_floats(repo):
    bundle = synth.bundle()
    frame = synth.telemetry(50)
    cols = frame.columns()
    cols["speed_kph"] = cols["speed_kph"].copy()
    cols["speed_kph"][[0, 7]] = np.nan
    cols["x"] = np.linspace(-1234.5678901234, 9876.54321, 50)
    tricky = models.TelemetryFrame.from_columns(cols)
    object.__setattr__(bundle, "telemetry", {("VER", 1): tricky})
    repo.save_session(bundle)
    _same_frame(repo.get_telemetry(synth.KEY, "VER", 1), tricky)


def test_list_laps_filters_by_driver_code_or_number(repo):
    repo.save_session(synth.bundle())
    assert {lap.lap_number for lap in repo.list_laps(synth.KEY, "VER")} == {1, 2, 3}
    assert {lap.lap_number for lap in repo.list_laps(synth.KEY, "nor")} == {1, 2}
    assert {lap.driver for lap in repo.list_laps(synth.KEY, "4")} == {"NOR"}
    assert repo.list_laps(synth.KEY, "XXX") == []


def test_laps_keep_their_stored_order(repo):
    bundle = synth.bundle()
    repo.save_session(bundle)
    assert [(l.driver, l.lap_number) for l in repo.list_laps(synth.KEY)] == [(l.driver, l.lap_number) for l in bundle.laps]


# ---------------------------------------------------------------- absence
def test_missing_session_and_telemetry_raise(repo):
    assert not repo.has_session("2024_06_Q")
    for call in (lambda: repo.get_session_info("2024_06_Q"), lambda: repo.list_laps("2024_06_Q"),
                 lambda: repo.list_drivers("2024_06_Q"), lambda: repo.get_corners("2024_06_Q"),
                 lambda: repo.get_weather("2024_06_Q"), lambda: repo.get_telemetry("2024_06_Q", "VER", 1)):
        with pytest.raises(SessionNotFoundError):
            call()
    repo.save_session(synth.bundle())
    with pytest.raises(TelemetryUnavailableError):
        repo.get_telemetry(synth.KEY, "NOR", 2)          # the lap exists, its telemetry was not stored
    with pytest.raises(TelemetryUnavailableError):
        repo.get_telemetry(synth.KEY, "VER", 99)         # no such lap


def test_stale_schema_version_counts_as_absent(repo, monkeypatch):
    repo.save_session(synth.bundle())
    module = sys.modules[type(repo).__module__]
    monkeypatch.setattr(module, "SCHEMA_VERSION", models.SCHEMA_VERSION + 1)
    assert not repo.has_session(synth.KEY)
    assert repo.get_manifest(synth.KEY).schema_version == models.SCHEMA_VERSION
    assert repo.list_sessions() == []
    with pytest.raises(SessionNotFoundError):
        repo.list_laps(synth.KEY)


# ---------------------------------------------------------------- listing
def test_list_sessions_sorted_and_filtered_by_year(repo):
    repo.save_session(synth.bundle("2024_05_Q"))
    repo.save_session(synth.bundle("2023_01_R"))
    repo.save_session(synth.bundle("2024_02_R"))
    assert [s.key for s in repo.list_sessions()] == ["2023_01_R", "2024_02_R", "2024_05_Q"]
    assert [s.key for s in repo.list_sessions(2024)] == ["2024_02_R", "2024_05_Q"]
    assert repo.list_sessions(2019) == []


# ---------------------------------------------------------------- idempotent re-ingest
def test_saving_the_same_session_twice_is_idempotent(repo):
    repo.save_session(synth.bundle())
    repo.save_session(synth.bundle())
    assert len(repo.list_laps(synth.KEY)) == 5
    assert len(repo.list_drivers(synth.KEY)) == 2
    assert len(repo.get_weather(synth.KEY)) == 1
    assert [s.key for s in repo.list_sessions()] == [synth.KEY]
    assert repo.get_manifest(synth.KEY).telemetry_count == 3


def test_overwrite_replaces_previous_session(repo):
    repo.save_session(synth.bundle())
    smaller = synth.bundle()
    object.__setattr__(smaller, "telemetry", {("VER", 1): synth.telemetry()})
    object.__setattr__(smaller, "laps", smaller.laps[:2])
    repo.save_session(smaller)
    assert len(repo.list_laps(synth.KEY)) == 2
    with pytest.raises(TelemetryUnavailableError):
        repo.get_telemetry(synth.KEY, "VER", 2)
    _same_frame(repo.get_telemetry(synth.KEY, "VER", 1), synth.telemetry())


def test_a_failed_save_leaves_the_previous_session_intact(repo):
    repo.save_session(synth.bundle())
    before = repo.list_laps(synth.KEY)
    broken = synth.bundle()
    object.__setattr__(broken, "telemetry", {**broken.telemetry, ("../x", 1): synth.telemetry()})
    with pytest.raises(ValueError):
        repo.save_session(broken)
    assert repo.has_session(synth.KEY)
    assert repo.list_laps(synth.KEY) == before
    _same_frame(repo.get_telemetry(synth.KEY, "VER", 2), synth.telemetry(310))


# ---------------------------------------------------------------- schedule cache
def test_events_cache_round_trip(repo):
    event = models.EventInfo(
        year=2024, round=5, event_name="Chinese Grand Prix", country="China", location="Shanghai",
        event_format="sprint_qualifying", event_date=datetime(2024, 4, 21, tzinfo=timezone.utc),
        sessions=(models.EventSession(name="Qualifying", code="Q", date=datetime(2024, 4, 20, 7, tzinfo=timezone.utc)),),
    )
    assert repo.get_events(2024) is None
    repo.save_events(2024, [event])
    fetched_at, events = repo.get_events(2024)
    assert events == [event] and fetched_at.tzinfo is not None
    repo.save_events(2024, [])                           # replaced, not appended
    assert repo.get_events(2024)[1] == []


def test_stale_events_schema_counts_as_absent(repo, monkeypatch):
    repo.save_events(2024, [])
    module = sys.modules[type(repo).__module__]
    monkeypatch.setattr(module, "SCHEMA_VERSION", models.SCHEMA_VERSION + 1)
    assert repo.get_events(2024) is None


# ---------------------------------------------------------------- safety
@pytest.mark.parametrize("key", ["../etc", "2024_5_Q", "2024_05_q", "2024_05_Q/..", "", "2024_05_Q'; DROP TABLE laps;--"])
def test_rejects_unsafe_keys(repo, key):
    with pytest.raises(ValueError):
        repo.has_session(key)


def test_rejects_unsafe_driver_codes(repo):
    repo.save_session(synth.bundle())
    with pytest.raises(ValueError):
        repo.get_telemetry(synth.KEY, "../x", 1)
