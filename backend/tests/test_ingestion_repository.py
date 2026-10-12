"""Phase 1 — ParquetRepository: round trips, atomic writes, manifest/schema invalidation."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import numpy as np
import pytest
from pathlib import Path

from app.ingestion import models
from app.ingestion.errors import SessionNotFoundError, TelemetryUnavailableError
from app.storage import repository as repo_mod
from app.storage.repository import ParquetRepository, atomic_write_bytes, telemetry_filename
from tests import ingestion_synth as synth


@pytest.fixture
def repo(tmp_path):
    return ParquetRepository(tmp_path / "data")


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

    tel = repo.get_telemetry(synth.KEY, "VER", 2)
    expected = bundle.telemetry[("VER", 2)]
    for name in models.TELEMETRY_COLUMNS:
        np.testing.assert_array_equal(getattr(tel, name), getattr(expected, name))
        assert getattr(tel, name).dtype == getattr(expected, name).dtype


def test_layout_on_disk(repo):
    repo.save_session(synth.bundle())
    root = repo.root / synth.KEY
    assert sorted(p.name for p in (root / "telemetry").iterdir()) == ["NOR_001.parquet", "VER_001.parquet", "VER_002.parquet"]
    for name in ("manifest.json", "session.json", "drivers.json", "laps.parquet", "weather.parquet",
                 "corners.parquet", "corners_raw.json"):
        assert (root / name).exists(), name
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["schema_version"] == models.SCHEMA_VERSION
    assert manifest["fastf1_version"] == "3.5.3"
    assert (manifest["lap_count"], manifest["telemetry_count"], manifest["driver_count"]) == (5, 3, 2)
    assert manifest["ingested_at"].endswith("Z") or "+00:00" in manifest["ingested_at"]
    # no staging/trash/temp debris left behind
    assert [p.name for p in repo.root.iterdir()] == [synth.KEY]


def test_list_laps_filters_by_driver_code_or_number(repo):
    repo.save_session(synth.bundle())
    assert {lap.lap_number for lap in repo.list_laps(synth.KEY, "VER")} == {1, 2, 3}
    assert {lap.lap_number for lap in repo.list_laps(synth.KEY, "nor")} == {1, 2}
    assert {lap.driver for lap in repo.list_laps(synth.KEY, "4")} == {"NOR"}


def test_missing_session_and_telemetry_raise(repo):
    with pytest.raises(SessionNotFoundError):
        repo.get_session_info("2024_06_Q")
    repo.save_session(synth.bundle())
    with pytest.raises(TelemetryUnavailableError):
        repo.get_telemetry(synth.KEY, "NOR", 2)  # lap exists, telemetry did not


def test_list_sessions_filters_year_and_ignores_debris(repo):
    repo.save_session(synth.bundle("2024_05_Q"))
    repo.save_session(synth.bundle("2023_01_R"))
    (repo.root / ".staging-2024_09_Q-dead").mkdir()
    (repo.root / "2024_07_Q").mkdir()  # no manifest = incomplete
    assert [s.key for s in repo.list_sessions()] == ["2023_01_R", "2024_05_Q"]
    assert [s.key for s in repo.list_sessions(2024)] == ["2024_05_Q"]


def test_overwrite_replaces_previous_session(repo):
    repo.save_session(synth.bundle())
    smaller = synth.bundle()
    object.__setattr__(smaller, "telemetry", {("VER", 1): synth.telemetry()})
    repo.save_session(smaller)
    assert [p.name for p in (repo.root / synth.KEY / "telemetry").iterdir()] == ["VER_001.parquet"]
    assert sorted(p.name for p in repo.root.iterdir()) == [synth.KEY]


# ---------------------------------------------------------------- atomicity
def test_atomic_write_failure_keeps_old_file_and_no_temp(tmp_path, monkeypatch):
    target = tmp_path / "file.json"
    atomic_write_bytes(target, b"old")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(repo_mod.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.json"]


def test_failed_save_leaves_previous_session_intact(repo, monkeypatch):
    repo.save_session(synth.bundle())
    before = repo.list_laps(synth.KEY)

    def broken(self, target, bundle):
        (target / "laps.parquet").parent.mkdir(parents=True, exist_ok=True)
        (target / "laps.parquet").write_bytes(b"partial")
        raise RuntimeError("crashed mid-ingest")

    monkeypatch.setattr(ParquetRepository, "_write_bundle", broken)
    with pytest.raises(RuntimeError):
        repo.save_session(synth.bundle())
    assert repo.has_session(synth.KEY)
    assert repo.list_laps(synth.KEY) == before
    assert [p.name for p in repo.root.iterdir()] == [synth.KEY]


def test_crash_before_manifest_means_not_present(repo, monkeypatch):
    real = repo_mod.atomic_write_json

    def no_manifest(path, payload):
        if path.name == "manifest.json":
            raise KeyboardInterrupt
        real(path, payload)

    monkeypatch.setattr(repo_mod, "atomic_write_json", no_manifest)
    with pytest.raises(KeyboardInterrupt):
        repo.save_session(synth.bundle())
    assert not repo.has_session(synth.KEY)
    assert list(repo.root.iterdir()) == []


# ---------------------------------------------------------------- manifest / schema version
def test_stale_schema_version_counts_as_absent(repo, monkeypatch):
    repo.save_session(synth.bundle())
    assert repo.has_session(synth.KEY)
    monkeypatch.setattr(repo_mod, "SCHEMA_VERSION", models.SCHEMA_VERSION + 1)
    assert not repo.has_session(synth.KEY)
    assert repo.get_manifest(synth.KEY).schema_version == models.SCHEMA_VERSION
    with pytest.raises(SessionNotFoundError):
        repo.list_laps(synth.KEY)


def test_corrupt_manifest_counts_as_absent(repo):
    repo.save_session(synth.bundle())
    (repo.root / synth.KEY / "manifest.json").write_text("{not json")
    assert repo.get_manifest(synth.KEY) is None
    assert not repo.has_session(synth.KEY)


# ---------------------------------------------------------------- schedule cache / safety
def test_events_cache_round_trip(repo):
    event = models.EventInfo(
        year=2024, round=5, event_name="Chinese Grand Prix", country="China", location="Shanghai",
        event_format="sprint_qualifying", event_date=datetime(2024, 4, 21, tzinfo=timezone.utc),
        sessions=(models.EventSession(name="Qualifying", code="Q",
                                      date=datetime(2024, 4, 20, 7, tzinfo=timezone.utc)),),
    )
    assert repo.get_events(2024) is None
    repo.save_events(2024, [event])
    fetched_at, events = repo.get_events(2024)
    assert events == [event] and fetched_at.tzinfo is not None


@pytest.mark.parametrize("key", ["../etc", "2024_5_Q", "2024_05_q", "2024_05_Q/..", ""])
def test_rejects_unsafe_keys(repo, key):
    with pytest.raises(ValueError):
        repo.has_session(key)


def test_rejects_unsafe_driver_codes():
    assert telemetry_filename("VER", 7) == "VER_007.parquet"
    with pytest.raises(ValueError):
        telemetry_filename("../x", 1)


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        models.SessionInfo.model_validate({**synth.bundle().info.model_dump(), "date": datetime(2024, 4, 20, 7)})


def test_atomic_write_creates_parents(tmp_path):
    atomic_write_bytes(tmp_path / "a" / "b" / "c.bin", b"x")
    assert (tmp_path / "a" / "b" / "c.bin").read_bytes() == b"x"
    assert os.listdir(tmp_path / "a" / "b") == ["c.bin"]


def test_swap_retries_a_transient_permission_error(repo, monkeypatch):
    real = repo_mod.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        if Path(src).name.startswith(".staging-") and calls["n"] < 2:  # the directory swap only
            calls["n"] += 1
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(repo_mod.os, "replace", flaky)
    monkeypatch.setattr(repo_mod.time, "sleep", lambda s: None)
    repo.save_session(synth.bundle())
    assert calls["n"] == 2 and repo.has_session(synth.KEY)


def test_swap_gives_up_on_a_persistent_lock_and_keeps_the_old_copy(repo, monkeypatch):
    repo.save_session(synth.bundle())
    real = repo_mod.os.replace

    def locked(src, dst):
        if Path(src).name.startswith(".staging-"):
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(repo_mod.os, "replace", locked)
    monkeypatch.setattr(repo_mod.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        repo.save_session(synth.bundle())
    assert repo.has_session(synth.KEY)  # the previous copy was restored
