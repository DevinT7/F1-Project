"""Phase 2 real-data check: run compare_laps over many valid-lap pairs of an ingested
session and report the final-delta error and sector cross-check distributions.

    cd backend && python -m scripts.alignment_report --key 2024_05_Q [--max-pairs 3000]

Needs the session in the local store (python -m app.ingestion ingest ...).
"""
from __future__ import annotations

import argparse
import itertools
import random
from collections import Counter

import numpy as np

from app.config import settings
from app.ingestion.errors import IngestionError
from app.processing.delta import compare_laps, sector_check
from app.processing.models import LapData
from app.storage.repository import ParquetRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", required=True)
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--max-pairs", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    repo = ParquetRepository(args.data_dir)
    laps = [lap for lap in repo.list_laps(args.key) if lap.is_valid_for_comparison]
    data = {(l.driver, l.lap_number): LapData.from_repository(repo, args.key, l.driver, l.lap_number) for l in laps}
    pairs = list(itertools.combinations(sorted(data), 2))
    random.Random(args.seed).shuffle(pairs)
    pairs = pairs[: args.max_pairs]

    errors: list[float] = []
    sector_disc: list[float] = []
    sector_ok = sector_total = 0
    failures: Counter[str] = Counter()
    for ref_key, tgt_key in pairs:
        try:
            cmp = compare_laps(data[ref_key], data[tgt_key])
        except IngestionError as exc:
            failures[type(exc).__name__] += 1
            continue
        errors.append(cmp.quality.final_delta_error_s)
        diag = sector_check(cmp)
        if diag.available:
            sector_total += 1
            sector_ok += diag.ok
            sector_disc.extend(c.discrepancy_s for c in diag.checks[:2])

    e = np.abs(np.array(errors))
    s = np.abs(np.array(sector_disc))
    print(f"{args.key}: {len(laps)} valid laps, {len(pairs)} pairs tested")
    print(f"  compared OK: {len(errors)}   raised: {dict(failures) or 0}")
    if len(e):
        print(f"  |final delta error| s: median {np.median(e):.6f}  p95 {np.percentile(e, 95):.6f}  max {e.max():.6f}")
        print(f"  within 0.02 s: {np.mean(e <= 0.02):.1%} of compared pairs, "
              f"{(e <= 0.02).sum() / len(pairs):.1%} of all tested pairs")
    if len(s):
        print(f"  sector boundary |discrepancy| s: median {np.median(s):.4f}  p95 {np.percentile(s, 95):.4f}  max {s.max():.4f}")
        print(f"  sector check ok (all boundaries within 0.03 s): {sector_ok}/{sector_total} = {sector_ok / sector_total:.1%}")


if __name__ == "__main__":
    main()
