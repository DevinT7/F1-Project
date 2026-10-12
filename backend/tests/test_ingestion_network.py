"""Phase 1 — live FastF1 integration. Excluded by default; run with:

    cd backend && pytest -m network tests/test_ingestion_network.py

Uses the persistent FastF1 cache (FASTF1_CACHE_DIR / RACEIQ_FASTF1_CACHE_DIR, default
.fastf1_cache) so only the first run downloads. A full quali ingest takes ~2.5 min
because Lap.get_telemetry() costs ~0.5 s per lap.
"""
from __future__ import annotations

import os

import pytest

from app.config import settings
from app.ingestion.fastf1_client import FastF1Client
from app.ingestion.models import TELEMETRY_COLUMNS
from app.ingestion.service import IngestionService, get_fastest_valid_lap
from app.storage.repository import ParquetRepository

pytestmark = pytest.mark.network

CACHE = os.getenv("FASTF1_CACHE_DIR") or os.getenv("RACEIQ_FASTF1_CACHE_DIR") or settings.fastf1_cache_dir


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    repo = ParquetRepository(tmp_path_factory.mktemp("store"))
    result = IngestionService(repo, FastF1Client(CACHE)).ingest(2024, 5, "Q")
    return repo, result


def test_qualifying_counts(ingested):
    repo, result = ingested
    assert result.key == "2024_05_Q" and not result.skipped
    assert len(repo.list_drivers(result.key)) == 20
    assert result.laps == 279
    assert result.telemetry_laps == 279
    assert result.valid_laps > 60


def test_fastest_valid_lap_matches_official_within_1ms(ingested):
    repo, result = ingested
    for driver in repo.list_drivers(result.key):
        official = min(t for t in (driver.q1_s, driver.q2_s, driver.q3_s) if t is not None)
        ours = get_fastest_valid_lap(repo, result.key, driver.code).lap_time_s
        assert ours == pytest.approx(official, abs=0.001), driver.code


def test_telemetry_has_all_channels(ingested):
    repo, result = ingested
    lap = get_fastest_valid_lap(repo, result.key, "VER")
    tel = repo.get_telemetry(result.key, "VER", lap.lap_number)
    assert set(tel.columns()) == set(TELEMETRY_COLUMNS)
    assert len(tel) >= 200 and tel.drs_open.any() and tel.brake.any()


def test_reingest_is_a_noop(ingested):
    repo, _ = ingested
    assert IngestionService(repo, FastF1Client(CACHE)).ingest(2024, 5, "Q").skipped
