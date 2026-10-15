"""Mutation-style sanity checks (Phase 9): plant a bug in a critical formula and confirm the tests
catch it. A mutation the tests do not catch ("survives") means a formula is unguarded.

    cd backend && python -m scripts.mutation_sanity [--only delta_sign ...]

Each mutation is applied to a throwaway copy of the backend (the working tree is never touched),
then the tests that guard that formula run against the copy; they must FAIL. The 7B validator
has its own, larger mutation suite (scripts/validator_mutation_test.py).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".hypothesis",
                                ".fastf1_cache", "*.pyc")


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    old: str
    new: str
    tests: tuple[str, ...]
    why: str


MUTATIONS = (
    Mutation("delta_sign", "app/processing/delta.py",
             "delta: FloatArray = (target.time_s - reference.time_s).astype(np.float64)",
             "delta: FloatArray = (reference.time_s - target.time_s).astype(np.float64)",
             ("tests/test_processing_delta.py", "tests/test_golden_findings.py"), "the core sign convention (target − reference)"),
    Mutation("sector_delta_sign", "app/processing/delta.py",
             "sector_deltas = (tgt[0] - ref[0], tgt[1] - ref[1], tgt[2] - ref[2])",
             "sector_deltas = (ref[0] - tgt[0], tgt[1] - ref[1], tgt[2] - ref[2])",
             ("tests/test_processing_delta.py", "tests/test_golden_findings.py"), "the official sector cross-check"),
    Mutation("entry_loss_sign", "app/processing/insights.py",
             "time_loss_s=loss, entry_loss_s=t_apex - t_start, exit_loss_s=t_end - t_apex,",
             "time_loss_s=loss, entry_loss_s=t_start - t_apex, exit_loss_s=t_end - t_apex,",
             ("tests/test_processing_insights.py", "tests/test_golden_findings.py"), "corner entry/exit split"),
    Mutation("pit_window_crossing", "app/tires/pit_window.py",
             "hit = np.flatnonzero(values >= threshold)",
             "hit = np.flatnonzero(values <= threshold)",
             ("tests/test_tires_pit_window.py",), "the pit-window threshold crossing"),
    Mutation("conformal_rank", "app/tires/models/intervals.py",
             "rank = math.ceil((n + 1) * (1 - alpha))",
             "rank = math.ceil((n + 1) * alpha)",
             ("tests/test_tires_intervals.py",), "the split-conformal quantile (interval coverage)"),
    Mutation("display_magnitude", "app/ai/numbers.py",
             "magnitude = abs(float(value))",
             "magnitude = float(value)",
             ("tests/test_ai_numbers.py", "tests/test_ai_validator.py"), "display strings are magnitudes"),
    Mutation("cache_key_ingest", "app/api/services.py",
             "digest_ = digest(SCHEMA_VERSION, FINDINGS_SCHEMA_VERSION, tables.ingested_at.isoformat(), ref_rec.driver,",
             "digest_ = digest(SCHEMA_VERSION, FINDINGS_SCHEMA_VERSION, ref_rec.driver,",
             ("tests/test_cache.py", "-k", "invalidation_on_reingest"), "re-ingest must invalidate cached comparisons"),
    Mutation("token_revocation", "app/auth/deps.py",
             "if user is None or user.token_version != claims.version:",
             "if user is None:",
             ("tests/test_auth.py",), "logout / reuse detection revoke tokens"),
    Mutation("single_flight_lock", "app/caching.py",
             "        if cache.add(lock, token, lock_ttl_s):",
             "        if True:",
             ("tests/test_cache.py",), "identical concurrent requests compute once"),
)


def run(mutation: Mutation, root: Path) -> tuple[bool, float, str]:
    """(killed, seconds, last output line)."""
    target = root / mutation.file
    original = target.read_text(encoding="utf-8")
    if original.count(mutation.old) != 1:
        return False, 0.0, f"MUTATION NOT APPLICABLE: the code changed ({mutation.file})"
    target.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
    start = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
                               f"--basetemp={root / '.pytest-tmp'}", *mutation.tests],
                              cwd=root, capture_output=True, text=True, timeout=1800)
    finally:
        target.write_text(original, encoding="utf-8")
    lines = [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    # caught = a test ASSERTION failed; a missing file, a collection error or "no tests ran" proves nothing
    killed = proc.returncode == 1 and " failed" in last and "error" not in last.lower()
    return killed, time.perf_counter() - start, last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.mutation_sanity")
    parser.add_argument("--only", nargs="*", help="mutation names")
    args = parser.parse_args(argv)
    chosen = [m for m in MUTATIONS if not args.only or m.name in args.only]
    with tempfile.TemporaryDirectory(prefix="mut") as tmp:
        root = Path(tmp) / "backend"
        shutil.copytree(BACKEND, root, ignore=IGNORE)
        survived = []
        for m in chosen:
            killed, seconds, last = run(m, root)
            status = "killed  " if killed else "SURVIVED"
            print(f"{status} {m.name:22} {seconds:6.1f} s  {m.why}  [{last[:90]}]", flush=True)
            if not killed:
                survived.append(m.name)
    print(f"\n{len(chosen) - len(survived)}/{len(chosen)} mutations caught by the tests"
          + (f"; SURVIVED: {', '.join(survived)}" if survived else ""))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
