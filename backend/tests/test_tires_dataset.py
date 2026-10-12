"""Phase 6A — dataset builder: rows, derived columns, invariants, hashing, logging, outputs."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.storage.repository import ParquetRepository
from app.tires import dataset as D
from app.tires.config import DATASET_COLUMNS, NO_NEUTRALIZATION_SENTINEL, ExclusionConfig
from tests import tires_synth as S


@pytest.fixture
def repo(tmp_path):
    r = ParquetRepository(tmp_path / "data")
    S.store_race(r, "2023_01_R")
    S.store_race(r, "2024_02_R")
    return r


def test_rows_and_exclusions_on_a_synthetic_race(repo):
    result = D.build_dataset(repo, ["2023_01_R"])
    frame = result.frame
    assert list(frame.columns) == list(DATASET_COLUMNS)
    race = result.log["per_race"]["2023_01_R"]
    # per driver: lap1, SC lap 6, in-lap 10, out-lap 11, outlier lap 16 → 15 of 20 laps kept
    assert race["drops"] == {"lap_1_or_restart": 2, "in_or_out_lap": 4, "non_green_track_status": 2,
                             "inaccurate_or_deleted": 0, "wet_conditions": 0, "unknown_compound": 0,
                             "stint_outlier": 2, "short_stint": 0}
    assert race["input_rows"] == 40 and race["kept_rows"] == 30 == len(frame)
    assert race["input_rows"] == race["kept_rows"] + sum(race["drops"].values())


def test_derived_columns(repo):
    ver = D.build_dataset(repo, ["2023_01_R"]).frame.query("driver == 'VER'").set_index("lap_number")
    # stint_lap_index counts CLEAN laps; tyre_age keeps the original (gap at the SC lap is visible)
    assert ver.loc[5, ["tyre_age", "stint_lap_index"]].tolist() == [5, 4]
    assert ver.loc[7, ["tyre_age", "stint_lap_index"]].tolist() == [7, 5]
    # previous CLEAN lap of the same stint; null at the start of each stint
    assert pd.isna(ver.loc[2, "prev_lap_time_s"]) and pd.isna(ver.loc[12, "prev_lap_time_s"])
    assert ver.loc[7, "prev_lap_time_s"] == pytest.approx(ver.loc[5, "lap_time_s"])
    # SC on lap 6: before it the sentinel, after it laps since
    assert ver.loc[5, "laps_since_neutralization"] == NO_NEUTRALIZATION_SENTINEL
    assert ver.loc[9, "laps_since_neutralization"] == 3
    assert ver.loc[12, "fuel_proxy"] == 20 - 12 and (ver["race_laps_total"] == 20).all()
    assert not ver["traffic_known"].any()
    assert ver.loc[12, ["compound", "stint", "circuit", "year", "round"]].tolist() == ["HARD", 2, "Testville", 2023, 1]


def test_invariants(repo):
    frame = D.build_dataset(repo, ["2023_01_R", "2024_02_R"]).frame
    assert frame["lap_time_s"].notna().all()
    assert (frame["tyre_age"] >= 0).all()
    assert frame["stint_lap_index"].min() == 1


def test_no_row_has_a_non_green_status(repo, tmp_path):
    laps = S.race_laps("2024_03_R")
    laps = [l.model_copy(update={"track_status": "12"}) if l.lap_number in (3, 4) else l for l in laps]
    S.store_race(repo, "2024_03_R", laps=laps)
    frame = D.build_dataset(repo, ["2024_03_R"]).frame
    assert not frame["lap_number"].isin([3, 4]).any()


def test_hash_is_stable_and_changes_with_config_or_sessions(repo):
    a = D.dataset_hash(["2024_02_R", "2023_01_R"], ExclusionConfig())
    assert a == D.dataset_hash(["2023_01_R", "2024_02_R"], ExclusionConfig())  # order-independent
    assert a == D.build_dataset(repo, ["2023_01_R", "2024_02_R"]).hash
    assert a != D.dataset_hash(["2023_01_R", "2024_02_R"], ExclusionConfig(outlier_mad_k=4.0))
    assert a != D.dataset_hash(["2023_01_R", "2024_02_R"], ExclusionConfig(keep_short_stints=True))
    assert a != D.dataset_hash(["2023_01_R"], ExclusionConfig())


def test_every_requested_session_is_accounted_for(repo):
    S.store_race(repo, "2024_04_Q", code="Q")
    wet = [l.model_copy(update={"rainfall": True}) for l in S.race_laps("2024_05_R")]
    S.store_race(repo, "2024_05_R", laps=wet)
    result = D.build_dataset(repo, ["2023_01_R", "2024_04_Q", "2024_05_R", "2024_09_R"])
    assert result.meta["included_sessions"] == ["2023_01_R"]
    assert result.meta["dropped_sessions"] == {
        "2024_04_Q": "not_a_race (Q)", "2024_05_R": "no_clean_laps", "2024_09_R": "not_ingested",
    }
    # 20 laps, one driver: lap 1, the in- and out-lap and the SC lap go first → 16 left for wet_conditions
    assert result.log["per_race"]["2024_05_R"]["drops"]["wet_conditions"] == 16


def test_race_laps_total_falls_back_to_the_longest_lap(repo):
    S.store_race(repo, "2024_06_R", total_laps=None)
    frame = D.build_dataset(repo, ["2024_06_R"]).frame
    assert (frame["race_laps_total"] == 20).all()


def test_outputs_and_meta(repo, tmp_path):
    result = D.build_dataset(repo, ["2023_01_R", "2024_02_R"])
    paths = D.write_outputs(result, tmp_path / "out")
    short = result.hash[:12]
    assert {p.name for p in paths.values()} == {f"dataset_{short}.parquet", f"exclusion_log_{short}.json",
                                                 f"dataset_{short}.meta.json"}
    assert len(pd.read_parquet(paths["dataset"])) == 60
    meta = json.loads(paths["meta"].read_text())
    assert meta["row_count"] == 60 and meta["seasons"] == [2023, 2024]
    assert meta["fastf1_versions"] == ["3.5.3"] and meta["schema_version"] == 1
    assert meta["git_sha"] is None or len(meta["git_sha"]) == 40
    log = json.loads(paths["exclusion_log"].read_text())
    assert log["input_rows"] == log["kept_rows"] + sum(log["totals"].values())


def test_race_sessions_lists_only_races(repo):
    S.store_race(repo, "2024_04_Q", code="Q")
    assert D.race_sessions(repo, [2024]) == ["2024_02_R"]
    assert D.race_sessions(repo, [2023, 2024]) == ["2023_01_R", "2024_02_R"]


def test_cli_build(repo, tmp_path, capsys):
    assert D.main(["build", "--seasons", "2023", "2024", "--data-dir", str(repo.root), "--out-dir", str(tmp_path / "o")]) == 0
    out = capsys.readouterr().out
    assert "60 rows from 2 races" in out and "per-rule drops" in out
