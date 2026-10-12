"""Phase 1 — IngestionService: orchestration, idempotency, schedule cache, fastest lap."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.ingestion import models
from app.ingestion.errors import LapInvalidError, SessionNotFoundError, TelemetryUnavailableError
from app.ingestion.fastf1_client import FastF1Client
from app.ingestion.service import IngestionService, get_fastest_valid_lap
from app.storage import repository as repo_mod
from app.storage.repository import ParquetRepository
from tests import ingestion_synth as synth


def _source(missing: set[tuple[str, int]] = frozenset()):
    source = MagicMock(spec=FastF1Client)
    source.fastf1_version = "3.5.3"
    source.load_session.side_effect = lambda y, r, c: synth.raw_session(y, r, c)

    def telemetry(raw, driver, lap_number):
        if (driver, lap_number) in missing:
            raise TelemetryUnavailableError("gone")
        return synth.raw_telemetry_frame()

    source.lap_telemetry.side_effect = telemetry
    return source


@pytest.fixture
def repo(tmp_path):
    return ParquetRepository(tmp_path / "data")


def test_ingest_end_to_end_with_mock_source(repo):
    source = _source()
    result = IngestionService(repo, source).ingest(2024, 5, "Q")
    assert not result.skipped
    assert (result.key, result.laps, result.telemetry_laps, result.valid_laps) == ("2024_05_Q", 3, 3, 3)
    assert source.lap_telemetry.call_count == 3

    info = repo.get_session_info("2024_05_Q")
    assert info.date == datetime(2024, 4, 20, 7, tzinfo=timezone.utc)
    laps = repo.list_laps("2024_05_Q")
    assert all(lap.air_temp_c == 22.0 for lap in laps)
    assert len(repo.get_telemetry("2024_05_Q", "VER", 2)) == 400
    assert [d.team_color for d in repo.list_drivers("2024_05_Q")] == ["#3671C6", "#FF8000"]


def test_missing_telemetry_marks_lap_invalid_and_does_not_crash(repo):
    IngestionService(repo, _source(missing={("VER", 2)})).ingest(2024, 5, "Q")
    lap = next(lap for lap in repo.list_laps("2024_05_Q", "VER") if lap.lap_number == 2)
    assert not lap.is_valid_for_comparison and lap.invalid_reason == "no_telemetry"
    with pytest.raises(TelemetryUnavailableError):
        repo.get_telemetry("2024_05_Q", "VER", 2)


def test_second_ingest_makes_zero_network_calls(repo):
    IngestionService(repo, _source()).ingest(2024, 5, "Q")
    fresh = _source()
    result = IngestionService(repo, fresh).ingest(2024, 5, "Q")
    assert result.skipped
    assert fresh.mock_calls == []  # not even an attribute read on the client


def test_force_reingests(repo):
    IngestionService(repo, _source()).ingest(2024, 5, "Q")
    source = _source()
    assert not IngestionService(repo, source).ingest(2024, 5, "q", force=True).skipped
    source.load_session.assert_called_once()


def test_stale_schema_triggers_reingest(repo, monkeypatch):
    IngestionService(repo, _source()).ingest(2024, 5, "Q")
    monkeypatch.setattr(repo_mod, "SCHEMA_VERSION", models.SCHEMA_VERSION + 1)
    source = _source()
    assert not IngestionService(repo, source).ingest(2024, 5, "Q").skipped
    source.load_session.assert_called_once()


def test_session_not_found_propagates_and_stores_nothing(repo):
    source = _source()
    source.load_session.side_effect = SessionNotFoundError("nope")
    with pytest.raises(SessionNotFoundError):
        IngestionService(repo, source).ingest(2024, 99, "Q")
    assert repo.list_sessions() == []


# ---------------------------------------------------------------- fastest lap
def test_fastest_valid_lap_skips_invalid_laps(repo):
    repo.save_session(synth.bundle())
    # VER lap 3 (92.0) is faster but invalid; 93.66 on lap 2 is the answer.
    assert get_fastest_valid_lap(repo, synth.KEY, "VER").lap_number == 2
    assert get_fastest_valid_lap(repo, synth.KEY, "NOR").lap_time_s == 94.165


def test_fastest_valid_lap_raises_when_none(repo):
    bundle = synth.bundle()
    object.__setattr__(bundle, "laps", [synth.lap("VER", 1, 93.0, valid=False)])
    repo.save_session(bundle)
    with pytest.raises(LapInvalidError):
        get_fastest_valid_lap(repo, synth.KEY, "VER")
    with pytest.raises(LapInvalidError):
        get_fastest_valid_lap(repo, synth.KEY, "HAM")


# ---------------------------------------------------------------- schedule / season
def _schedule() -> pd.DataFrame:
    return pd.DataFrame({
        "RoundNumber": [0, 1, 2], "Country": ["Bahrain"] * 3, "Location": ["Sakhir"] * 3,
        "EventName": ["Testing", "Bahrain GP", "Future GP"], "EventFormat": ["testing", "conventional", "conventional"],
        "EventDate": [pd.Timestamp("2024-02-23"), pd.Timestamp("2024-03-02"), pd.Timestamp("2099-03-09")],
        "Session4": ["Qualifying"] * 3,
        "Session4DateUtc": [pd.Timestamp("2024-02-23"), pd.Timestamp("2024-03-01 16:00"), pd.Timestamp("2099-03-08")],
        "Session5": ["Race"] * 3,
        "Session5DateUtc": [pd.Timestamp("2024-02-23"), pd.Timestamp("2024-03-02 15:00"), pd.Timestamp("2099-03-09")],
    })


def test_list_events_caches_past_seasons_forever(repo):
    source = _source()
    source.event_schedule.return_value = _schedule()
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    service = IngestionService(repo, source, clock=lambda: now)
    assert [e.round for e in service.list_events(2024)] == [1, 2]
    later = IngestionService(repo, source, clock=lambda: now + timedelta(days=400))
    later.list_events(2024)
    source.event_schedule.assert_called_once()


def test_list_events_refreshes_current_season_after_ttl(repo):
    source = _source()
    source.event_schedule.return_value = _schedule()
    now = datetime.now(timezone.utc)
    IngestionService(repo, source, clock=lambda: now).list_events(now.year)
    IngestionService(repo, source, clock=lambda: now + timedelta(hours=1)).list_events(now.year)
    assert source.event_schedule.call_count == 1
    IngestionService(repo, source, clock=lambda: now + timedelta(hours=25)).list_events(now.year)
    assert source.event_schedule.call_count == 2


def test_ingest_season_skips_future_and_survives_failures(repo):
    source = _source()
    source.event_schedule.return_value = _schedule()

    def load(year, rnd, code):
        if code == "R":
            raise SessionNotFoundError("race data missing")
        return synth.raw_session(year, rnd, code)

    source.load_session.side_effect = load
    service = IngestionService(repo, source, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    results = service.ingest_season(2024, ["Q", "R"])
    assert [(r.key, r.error is None) for r in results] == [("2024_01_Q", True), ("2024_01_R", False)]
    assert repo.has_session("2024_01_Q")
