"""Phase 6A dataset builder: ingested race sessions → clean, logged, hashed training table.

    cd backend && python -m app.tires.dataset build --seasons 2023 2024

One row = one race lap for one driver that survived every exclusion rule (exclusions.py).
Outputs in data/tires/ (hash = SHA-256 of sorted input sessions + schema version + exclusion
config; filenames use its first 12 characters):

    dataset_{hash}.parquet        the table (columns: config.DATASET_COLUMNS)
    exclusion_log_{hash}.json     per-race input/kept rows and per-rule drop counts
    dataset_{hash}.meta.json      FastF1 version(s), schema version, git SHA, sessions, row count

Every requested session is accounted for: included, or listed in meta "dropped_sessions"
with a reason. Nothing is dropped silently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..config import settings
from ..ingestion.models import LapRecord, SessionInfo
from ..storage.repository import ParquetRepository, SessionRepository, atomic_write_bytes, atomic_write_json
from .config import (
    DATASET_COLUMNS, DATASET_SCHEMA_VERSION, NEUTRALIZATION_CODES, NO_NEUTRALIZATION_SENTINEL,
    ExclusionConfig,
)
from .exclusions import RULE_NAMES, apply_rules, normalize_compound

REPO_ROOT = Path(__file__).resolve().parents[3]


def laps_frame(laps: Sequence[LapRecord]) -> pd.DataFrame:
    """Stored LapRecords → the columns the rules and builder use. Compound normalized here, once."""
    rows = [{
        "session_key": lap.session_key, "driver": lap.driver, "team": lap.team, "lap_number": lap.lap_number,
        "stint": lap.stint, "compound": lap.compound, "tyre_age": lap.tyre_age, "fresh_tyre": lap.fresh_tyre,
        "lap_time_s": lap.lap_time_s, "pit_in_time_s": lap.pit_in_time_s, "pit_out_time_s": lap.pit_out_time_s,
        "track_status": lap.track_status, "is_accurate": lap.is_accurate, "deleted": lap.deleted,
        "rainfall": lap.rainfall, "position": lap.position, "air_temp_c": lap.air_temp_c,
        "track_temp_c": lap.track_temp_c,
    } for lap in laps]
    df = pd.DataFrame(rows, columns=[
        "session_key", "driver", "team", "lap_number", "stint", "compound", "tyre_age", "fresh_tyre",
        "lap_time_s", "pit_in_time_s", "pit_out_time_s", "track_status", "is_accurate", "deleted",
        "rainfall", "position", "air_temp_c", "track_temp_c",
    ])
    if not df.empty:
        df["compound"] = normalize_compound(df["compound"]).replace("", None)
    return df


def neutralized_laps(df: pd.DataFrame) -> list[int]:
    """Lap numbers on which ANY driver's lap carried a SC / VSC / red-flag code, sorted."""
    hit = df["track_status"].map(lambda s: bool(set(s) & NEUTRALIZATION_CODES) if isinstance(s, str) else False)
    return sorted({int(n) for n in df.loc[hit, "lap_number"]})


def laps_since(lap_numbers: "pd.Series[int]", neutral: list[int]) -> "pd.Series[int]":
    """Laps since the most recent earlier neutralized lap (sentinel when none yet)."""
    marks = np.asarray(neutral, dtype=np.int64)

    def one(n: int) -> int:
        idx = int(np.searchsorted(marks, n, side="left")) - 1
        return NO_NEUTRALIZATION_SENTINEL if idx < 0 else int(n - marks[idx])

    return lap_numbers.map(one).astype("int64")


def build_rows(info: SessionInfo, kept: pd.DataFrame, all_laps: pd.DataFrame) -> pd.DataFrame:
    """Derived columns on the laps that survived exclusion."""
    if kept.empty:
        return pd.DataFrame(columns=list(DATASET_COLUMNS))
    total = info.n_laps_scheduled or int(all_laps["lap_number"].max())
    df = kept.sort_values(["driver", "stint", "lap_number"]).copy()
    by_stint = df.groupby(["driver", "stint"], sort=False)
    df["stint_lap_index"] = by_stint.cumcount() + 1          # clean laps so far (gaps stay visible)
    df["prev_lap_time_s"] = by_stint["lap_time_s"].shift(1)  # previous CLEAN lap of the stint, else null
    df["laps_since_neutralization"] = laps_since(df["lap_number"], neutralized_laps(all_laps))
    df["circuit"] = info.circuit
    df["year"] = info.year
    df["round"] = info.round
    df["race_laps_total"] = int(total)
    df["fuel_proxy"] = int(total) - df["lap_number"]  # laps of fuel left; deliberately NOT converted to kg
    df["traffic_known"] = False  # no reliable gap data; never guessed
    out = df[list(DATASET_COLUMNS)].reset_index(drop=True)
    for col in ("stint", "tyre_age", "position"):
        out[col] = out[col].astype("Int64")
    return out


def dataset_hash(sessions: Sequence[str], cfg: ExclusionConfig) -> str:
    payload = json.dumps(
        {"sessions": sorted(sessions), "schema_version": DATASET_SCHEMA_VERSION, "exclusions": cfg.as_dict()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def git_sha() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class BuildResult:
    hash: str
    frame: pd.DataFrame
    log: dict[str, Any]
    meta: dict[str, Any]
    paths: dict[str, Path] = field(default_factory=dict)


def race_sessions(repo: SessionRepository, seasons: Sequence[int]) -> list[str]:
    """Stored race sessions (code R) of the given seasons, in key order."""
    return sorted(info.key for year in seasons for info in repo.list_sessions(year) if info.session_code == "R")


def validate(frame: pd.DataFrame) -> None:
    """Invariants every built dataset must satisfy; violating one is a bug, not a data issue."""
    if frame.empty:
        return
    if frame["lap_time_s"].isna().any():
        raise AssertionError("dataset contains a null lap_time_s")
    if (frame["tyre_age"].dropna() < 0).any():
        raise AssertionError("dataset contains a negative tyre_age")
    if (frame["stint_lap_index"] < 1).any():
        raise AssertionError("stint_lap_index must start at 1")


def build_dataset(
    repo: SessionRepository,
    sessions: Sequence[str],
    cfg: ExclusionConfig = ExclusionConfig(),
) -> BuildResult:
    digest = dataset_hash(sessions, cfg)
    frames: list[pd.DataFrame] = []
    per_race: dict[str, dict[str, Any]] = {}
    dropped: dict[str, str] = {}
    included: list[str] = []
    versions: set[str] = set()

    for key in sorted(sessions):
        manifest = repo.get_manifest(key)
        if manifest is None:
            dropped[key] = "not_ingested"
            continue
        if not repo.has_session(key):
            dropped[key] = "stale_schema"
            continue
        info = repo.get_session_info(key)
        if info.session_code != "R":
            dropped[key] = f"not_a_race ({info.session_code})"
            continue
        all_laps = laps_frame(repo.list_laps(key))
        result = apply_rules(all_laps, cfg)
        rows = build_rows(info, result.kept, all_laps)
        per_race[key] = {
            "event": info.event_name, "year": info.year, "round": info.round,
            "input_rows": len(all_laps), "kept_rows": len(rows),
            "retention": round(len(rows) / len(all_laps), 4) if len(all_laps) else 0.0,
            "drops": result.counts,
        }
        if rows.empty:
            dropped[key] = "no_clean_laps"
            continue
        frames.append(rows)
        included.append(key)
        versions.add(manifest.fastf1_version)

    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(DATASET_COLUMNS))
    validate(frame)
    totals = {name: sum(r["drops"][name] for r in per_race.values()) for name in RULE_NAMES}
    input_rows = sum(r["input_rows"] for r in per_race.values())
    if input_rows != len(frame) + sum(totals.values()):
        raise AssertionError("conservation violated: kept + dropped != input")
    log = {"hash": digest, "rules": list(RULE_NAMES), "totals": totals,
           "input_rows": input_rows, "kept_rows": len(frame), "per_race": per_race}
    meta = {
        "hash": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": DATASET_SCHEMA_VERSION,
        "fastf1_versions": sorted(versions),
        "git_sha": git_sha(),
        "exclusion_config": cfg.as_dict(),
        "requested_sessions": sorted(sessions),
        "included_sessions": included,
        "dropped_sessions": dropped,
        "seasons": sorted({int(k[:4]) for k in included}),
        "row_count": len(frame),
        "columns": list(DATASET_COLUMNS),
    }
    return BuildResult(hash=digest, frame=frame, log=log, meta=meta)


def write_outputs(result: BuildResult, out_dir: Path) -> dict[str, Path]:
    import io

    short = result.hash[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "dataset": out_dir / f"dataset_{short}.parquet",
        "exclusion_log": out_dir / f"exclusion_log_{short}.json",
        "meta": out_dir / f"dataset_{short}.meta.json",
    }
    buf = io.BytesIO()
    result.frame.to_parquet(buf, index=False)
    atomic_write_bytes(paths["dataset"], buf.getvalue())
    atomic_write_json(paths["exclusion_log"], result.log)
    atomic_write_json(paths["meta"], result.meta)
    result.paths = paths
    return paths


def _print_summary(result: BuildResult) -> None:
    log, meta = result.log, result.meta
    print(f"dataset {result.hash[:12]}: {meta['row_count']} rows from {len(meta['included_sessions'])} races "
          f"(seasons {meta['seasons']}); {log['input_rows']} input laps")
    width = max(len(n) for n in RULE_NAMES)
    print("\nper-rule drops (applied in this order):")
    for name in RULE_NAMES:
        n = log["totals"][name]
        print(f"  {name:<{width}}  {n:6d}  {100 * n / max(log['input_rows'], 1):5.1f}%")
    print(f"  {'kept':<{width}}  {log['kept_rows']:6d}  {100 * log['kept_rows'] / max(log['input_rows'], 1):5.1f}%")
    print("\nper-race retention:")
    for key, race in log["per_race"].items():
        print(f"  {key}  {race['event'][:28]:<28} {race['input_rows']:5d} -> {race['kept_rows']:5d}  ({100 * race['retention']:5.1f}%)")
    for key, reason in meta["dropped_sessions"].items():
        print(f"  {key}  DROPPED: {reason}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tires.dataset")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build the training dataset from ingested races")
    build.add_argument("--seasons", type=int, nargs="+", required=True)
    build.add_argument("--data-dir", default=settings.data_dir)
    build.add_argument("--out-dir", default=None, help="default: <data-dir>/tires")
    build.add_argument("--keep-short-stints", action="store_true", help="diagnostics only")
    args = parser.parse_args(argv)

    repo = ParquetRepository(args.data_dir)
    sessions = race_sessions(repo, args.seasons)
    if not sessions:
        print(f"no ingested race sessions for seasons {args.seasons}", file=sys.stderr)
        return 1
    result = build_dataset(repo, sessions, ExclusionConfig(keep_short_stints=args.keep_short_stints))
    paths = write_outputs(result, Path(args.out_dir) if args.out_dir else Path(args.data_dir) / "tires")
    _print_summary(result)
    print("\nwrote:", *[str(p) for p in paths.values()], sep="\n  ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
