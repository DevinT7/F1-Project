"""Benchmark compare_laps at 1 m resolution (target: < 50 ms per comparison).

    cd backend && python -m scripts.bench_alignment [--repeat 200]

Uses the real fixture (two ~5.4 km Shanghai quali laps, ~700 samples each) plus a
synthetic 25 Hz lap for a denser input.
"""
from __future__ import annotations

import argparse
import platform
import statistics
import time
from pathlib import Path

from app.processing.delta import compare_laps
from app.processing.models import LapData
from app.storage.repository import ParquetRepository
from tests.fixtures import synthetic as syn

STORE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ingestion" / "store"


def _bench(name: str, ref: LapData, tgt: LapData, repeat: int) -> None:
    compare_laps(ref, tgt)  # warm up
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        cmp = compare_laps(ref, tgt, step=1.0)
        times.append((time.perf_counter() - start) * 1000)
    times.sort()
    print(f"{name:32s} grid {len(cmp.grid_m):5d} pts  samples {len(ref.telemetry)}/{len(tgt.telemetry)}  "
          f"median {statistics.median(times):6.2f} ms  p95 {times[int(0.95 * len(times)) - 1]:6.2f} ms")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=200)
    args = parser.parse_args()
    print(f"python {platform.python_version()} on {platform.processor() or platform.machine()}")

    repo = ParquetRepository(STORE)
    valid = [l for l in repo.list_laps("2024_05_Q") if l.is_valid_for_comparison]
    ver = LapData.from_repository(repo, "2024_05_Q", valid[0].driver, valid[0].lap_number)
    alo = LapData.from_repository(repo, "2024_05_Q", valid[-1].driver, valid[-1].lap_number)
    _bench("real fixture (VER vs ALO)", ver, alo, args.repeat)

    profile = syn.make_profile()
    slow = syn.make_profile(slow_zone=syn.HAIRPIN_ZONE, slow_factor=0.9)
    _bench("synthetic 25 Hz vs 25 Hz", syn.sample_lap(profile, hz=25), syn.sample_lap(slow, hz=25, seed=1), args.repeat)


if __name__ == "__main__":
    main()
