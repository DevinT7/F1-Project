"""Golden findings (Phase 9): the full Phase 3 findings for five fixed comparisons, committed as
reviewed JSON so ANY change to alignment, deltas, corner segmentation or the rules shows up as a diff.

    cd backend && python -m scripts.golden_findings            # compare (what the test does)
    cd backend && python -m scripts.golden_findings --write    # re-record after REVIEWING the diff

Inputs: the committed Parquet fixture store (2024 R5 qualifying, the three valid laps VER 17,
PER 18, ALO 14). Floats are compared with an absolute tolerance of 1e-6 (BLAS / platform noise);
everything else must match exactly.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from pathlib import Path
from typing import Any

from app.processing.delta import compare_laps
from app.processing.insights import build_session_context, generate_findings
from app.processing.models import LapData
from app.storage.repository import ParquetRepository

BACKEND = Path(__file__).resolve().parents[1]
STORE = BACKEND / "tests" / "fixtures" / "ingestion" / "store"
GOLDEN = BACKEND / "tests" / "fixtures" / "golden"
KEY = "2024_05_Q"
LAPS = {"VER": 17, "PER": 18, "ALO": 14}
# (reference, target): ALO/VER both ways pins the sign convention under swapping
CASES = [("VER", "ALO"), ("ALO", "VER"), ("VER", "PER"), ("PER", "ALO"), ("ALO", "PER")]
ATOL = 1e-6


def findings(ref: str, tgt: str, repo: ParquetRepository | None = None) -> dict[str, Any]:
    repo = repo or ParquetRepository(STORE)
    info = repo.get_session_info(KEY)
    recs = {(l.driver, l.lap_number): l for l in repo.list_laps(KEY)}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cmp = compare_laps(LapData.from_repository(repo, KEY, ref, LAPS[ref]),
                           LapData.from_repository(repo, KEY, tgt, LAPS[tgt]))
    report = generate_findings(cmp, repo.get_corners(KEY),
                               context=build_session_context(recs[(ref, LAPS[ref])], recs[(tgt, LAPS[tgt])], info))
    data: dict[str, Any] = report.model_dump(mode="json")
    return data


def path_for(ref: str, tgt: str) -> Path:
    return GOLDEN / f"findings_{KEY}_{ref}_vs_{tgt}.json"


def diff(expected: Any, actual: Any, where: str = "") -> list[str]:
    """Paths where `actual` differs from `expected` (floats within ATOL are equal)."""
    out: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in actual:
                out.append(f"{where}/{k}: removed")
            elif k not in expected:
                out.append(f"{where}/{k}: added = {json.dumps(actual[k])[:80]}")
            else:
                out += diff(expected[k], actual[k], f"{where}/{k}")
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"{where}: length {len(expected)} → {len(actual)}")
        for i, (a, b) in enumerate(zip(expected, actual)):
            out += diff(a, b, f"{where}/{i}")
    elif isinstance(expected, float) or isinstance(actual, float):
        if not (isinstance(expected, (int, float)) and isinstance(actual, (int, float))
                and math.isclose(float(expected), float(actual), rel_tol=0.0, abs_tol=ATOL)):
            out.append(f"{where}: {expected!r} → {actual!r}")
    elif expected != actual:
        out.append(f"{where}: {json.dumps(expected)[:60]} → {json.dumps(actual)[:60]}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.golden_findings")
    parser.add_argument("--write", action="store_true", help="re-record the golden files (review the diff first)")
    args = parser.parse_args(argv)
    GOLDEN.mkdir(parents=True, exist_ok=True)
    repo = ParquetRepository(STORE)
    changed = 0
    for ref, tgt in CASES:
        actual = findings(ref, tgt, repo)
        path = path_for(ref, tgt)
        expected = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        problems = ["(new golden file)"] if expected is None else diff(expected, actual)
        if problems:
            changed += 1
            print(f"{path.name}: {len(problems)} difference(s)")
            for p in problems[:25]:
                print(f"   {p}")
        if args.write:
            path.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{changed} of {len(CASES)} comparisons differ" + (" (re-recorded)" if args.write and changed else ""))
    return 0 if args.write or changed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
