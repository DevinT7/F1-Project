"""Phase 6A — stint summaries and GET /api/v1/sessions/{key}/stints."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.storage.repository import ParquetRepository
from app.tires.stints import summarize_stints
from tests import tires_synth as S


def _short_third_stint(key: str) -> list:
    laps = S.race_laps(key)
    extra = [S.race_lap(key, "VER", n, 3, n - 20, 95.5, compound="soft", fresh_tyre=False) for n in range(21, 25)]
    return laps + extra


def test_eligibility_on_synthetic_stints():
    [ver] = summarize_stints(_short_third_stint("2024_01_R"))
    s1, s2, s3 = ver.stints
    # stint 1, laps 1-10: lap 1, the SC lap 6 and the in-lap 10 are excluded → 7 clean
    assert (s1.first_lap, s1.last_lap, s1.total_laps, s1.clean_laps, s1.model_eligible) == (1, 10, 10, 7, True)
    # stint 2, laps 11-20: out-lap 11 and the outlier lap 16 → 8 clean
    assert (s2.compound, s2.tyre_age_at_start, s2.clean_laps, s2.model_eligible) == ("HARD", 1, 8, True)
    # stint 3: four clean laps on a used set → counted, but not eligible (needs 5)
    assert (s3.compound, s3.fresh_tyre, s3.total_laps, s3.clean_laps, s3.model_eligible) == ("SOFT", False, 4, 4, False)


def test_exactly_five_clean_laps_is_eligible():
    laps = [S.race_lap("2024_01_R", "VER", n, 1, n, 95.0 + 0.01 * n) for n in range(2, 7)]
    [ver] = summarize_stints(laps)
    assert (ver.stints[0].clean_laps, ver.stints[0].model_eligible) == (5, True)


def test_stints_endpoint(tmp_path):
    repo = ParquetRepository(tmp_path / "data")
    S.store_race(repo, "2024_01_R", laps=_short_third_stint("2024_01_R") + S.race_laps("2024_01_R", "NOR"))
    app.dependency_overrides[deps.get_repository] = lambda: repo
    try:
        client = TestClient(app)
        body = client.get("/api/v1/sessions/2024-01-R/stints").json()
        assert body["session_key"] == "2024-01-R" and body["min_clean_laps"] == 5
        ver = next(d for d in body["drivers"] if d["driver"] == "VER")
        assert [s["model_eligible"] for s in ver["stints"]] == [True, True, False]
        missing = client.get("/api/v1/sessions/2024-09-R/stints")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "session_not_found"
    finally:
        app.dependency_overrides.clear()
