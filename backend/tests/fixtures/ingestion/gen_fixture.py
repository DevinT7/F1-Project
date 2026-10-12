"""Regenerate the trimmed real-data ingestion fixture (needs network or a warm FastF1 cache).

    cd backend && python -m tests.fixtures.ingestion.gen_fixture [--data-dir ../data]

Session 2024 R5 Qualifying (Chinese GP). Keeps 3 drivers x 3 laps: each driver's
fastest valid lap, their slowest other timed lap, and one lap without a lap time (an
out/in lap), so every common validity outcome is represented.

  raw/    FastF1-shaped frames exactly as the adapter returns them (normalize tests)
  store/  the same laps in ParquetRepository layout, validity computed against the FULL
          session (median distance etc.), so acceptance tests see production numbers.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from app.config import settings
from app.ingestion.fastf1_client import FastF1Client
from app.ingestion.models import SessionBundle
from app.ingestion.normalize import raw_records
from app.ingestion.service import IngestionService
from app.storage.repository import ParquetRepository

HERE = Path(__file__).resolve().parent
YEAR, ROUND, CODE, KEY = 2024, 5, "Q", "2024_05_Q"
DRIVERS = ("VER", "PER", "ALO")
RAW_TELEMETRY_COLUMNS = ["Time", "SessionTime", "Distance", "Speed", "RPM", "nGear", "Throttle",
                         "Brake", "DRS", "X", "Y", "Z", "Source"]


def _pick_laps(repo: ParquetRepository) -> dict[str, list[int]]:
    picks: dict[str, list[int]] = {}
    for driver in DRIVERS:
        laps = repo.list_laps(KEY, driver)
        valid = sorted((l for l in laps if l.is_valid_for_comparison), key=lambda l: l.lap_time_s or 0)
        timed = sorted((l for l in laps if l.lap_time_s is not None and l.lap_number != valid[0].lap_number),
                       key=lambda l: -(l.lap_time_s or 0))
        untimed = [l for l in laps if l.lap_time_s is None]
        picks[driver] = sorted({valid[0].lap_number, timed[0].lap_number, untimed[0].lap_number})
    return picks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--cache-dir", default=settings.fastf1_cache_dir)
    args = parser.parse_args()

    client = FastF1Client(args.cache_dir)
    full_repo = ParquetRepository(args.data_dir)
    IngestionService(full_repo, client).ingest(YEAR, ROUND, CODE)  # no-op if already stored
    picks = _pick_laps(full_repo)
    wanted = {(d, n) for d, laps in picks.items() for n in laps}
    print("picked", picks)

    # ---- raw frames
    raw = client.load_session(YEAR, ROUND, CODE)
    raw_dir = HERE / "raw"
    shutil.rmtree(raw_dir, ignore_errors=True)
    (raw_dir / "telemetry").mkdir(parents=True)
    mask = [(d, int(n)) in wanted for d, n in zip(raw.laps["Driver"], raw.laps["LapNumber"].fillna(-1))]
    laps = raw.laps.loc[mask].reset_index(drop=True)
    laps.drop(columns=["LapStartDate"]).to_parquet(raw_dir / "laps.parquet")
    raw.results[raw.results["Abbreviation"].isin(DRIVERS)].drop(columns=["HeadshotUrl"]).reset_index(
        drop=True).to_parquet(raw_dir / "results.parquet")
    raw.weather.to_parquet(raw_dir / "weather.parquet")
    raw.corners.to_parquet(raw_dir / "corners.parquet")
    info = {k: (str(v) if k == "date" else v) for k, v in raw.info.items()}
    (raw_dir / "info.json").write_text(json.dumps(info, indent=2, default=str))
    for driver, number in sorted(wanted):
        tel = client.lap_telemetry(raw, driver, number)[RAW_TELEMETRY_COLUMNS]
        tel.reset_index(drop=True).to_parquet(raw_dir / "telemetry" / f"{driver}_{number:03d}.parquet")

    # ---- store layout, trimmed from the full ingest
    store = ParquetRepository(HERE / "store")
    shutil.rmtree(store.root, ignore_errors=True)
    full = full_repo.get_session_info(KEY)
    bundle = SessionBundle(
        info=full,
        drivers=[d for d in full_repo.list_drivers(KEY) if d.code in DRIVERS],
        laps=[l for l in full_repo.list_laps(KEY) if (l.driver, l.lap_number) in wanted],
        weather=full_repo.get_weather(KEY),
        corners=full_repo.get_corners(KEY),
        telemetry={k: full_repo.get_telemetry(KEY, *k) for k in sorted(wanted)},
        corners_raw=raw_records(raw.corners),
        fastf1_version=raw.fastf1_version,
    )
    store.save_session(bundle)
    size = sum(p.stat().st_size for p in HERE.rglob("*") if p.is_file())
    print(f"fixture size {size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
