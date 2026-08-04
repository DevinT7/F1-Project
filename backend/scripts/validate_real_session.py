#!/usr/bin/env python3
"""
Phase 6 (Person 3): run the tire path against a **real** session and check the numbers.

Every other test in this repo runs on fixtures, which proves the maths is
self-consistent and proves nothing about whether it is *right*. Plausible-looking but
historically wrong output is the failure mode this script exists to catch, so it does
two things the offline suite can't:

1. runs `analyse_current_stint` for the whole grid and range-checks the result, and
2. prints each driver's **actual** stint lengths and pit laps from OpenF1 next to the
   prediction, so a human can see whether the recommended window lands anywhere near
   where that driver really stopped.

Default session is the 2023 Singapore GP (`session_key=9159`) — the one named in
P3_TIRE_AI_PLAN.md, a 62-lap race with a mid-race safety car, so the outlier path and
the pit-lap exclusions both get exercised on real data.

Needs network. Nothing in `pytest` depends on it.

    cd backend && python scripts/validate_real_session.py
    cd backend && python scripts/validate_real_session.py --session-key 9158 --laps 78
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clients.openf1 import get_client  # noqa: E402
from app.services import tires as tires_svc  # noqa: E402

# An F1 stint that is actually degrading sits in here. Outside it, either the fit is
# picking up something that isn't tyre wear, or the fuel correction is wrong.
PLAUSIBLE_DEG_S_PER_LAP = (0.005, 0.30)
# and the grid's *median* should sit in the range the project doc quotes
PLAUSIBLE_MEDIAN_DEG = (0.02, 0.15)


async def main(session_key: int, race_laps: int, horizon: int) -> int:
    client = get_client()
    drivers = await client.drivers(session_key=session_key)
    if not drivers:
        print(f"no drivers for session_key={session_key} — is it a real session?")
        return 1

    print(f"session_key={session_key}  drivers={len(drivers)}  race_laps={race_laps}\n")
    header = f"{'car':>4} {'cmp':>7} {'age':>4} {'s/lap':>7} {'left':>5} " \
             f"{'window':>9} {'conf':>5} {'model':>16}  actual pit laps"
    print(header)
    print("-" * len(header))

    t0 = time.monotonic()
    rows, failures, degradations = [], [], []

    for d in sorted(drivers, key=lambda x: x.get("driver_number") or 0):
        num = d.get("driver_number")
        try:
            a = await tires_svc.analyse_current_stint(session_key, num, horizon=horizon)
            stints = await client.stints(session_key=session_key, driver_number=num)
        except Exception as e:  # noqa: BLE001 — a crash on real data IS the finding
            failures.append(f"car {num}: {type(e).__name__}: {e}")
            continue

        # OpenF1 has no pit-lap field on stints; a stint ending at lap N means the
        # driver pitted on or about lap N.
        actual = [s.get("lap_end") for s in sorted(stints, key=lambda s: s.get("stint_number") or 0)][:-1]
        window = f"{a.recommended_pit_window[0]}-{a.recommended_pit_window[1]}" \
            if a.recommended_pit_window else "-"
        print(f"{num:>4} {str(a.compound)[:7]:>7} {a.tyre_age_laps or 0:>4} "
              f"{a.degradation_s_per_lap:>7.3f} "
              f"{'-' if a.remaining_competitive_laps is None else a.remaining_competitive_laps:>5} "
              f"{window:>9} {a.model_confidence:>5.2f} {a.model_name:>16}  "
              f"{', '.join(str(x) for x in actual if x) or '-'}")
        rows.append(a)
        if a.degradation_s_per_lap > 0:
            degradations.append(a.degradation_s_per_lap)

    elapsed = time.monotonic() - t0

    # Repeat calls must be cheap: P1's client caches OpenF1 and `_artifact` is
    # lru_cached, so a second sweep should be dominated by neither.
    t1 = time.monotonic()
    for d in drivers:
        await tires_svc.analyse_current_stint(session_key, d.get("driver_number"),
                                              horizon=horizon)
    warm = time.monotonic() - t1
    await client.aclose()

    print(f"\ncold sweep {elapsed:5.1f}s   warm sweep {warm:5.1f}s")

    # ------------------------------------------------------------------ checks
    problems = list(failures)
    if not rows:
        problems.append("no driver produced a TireAnalysis at all")

    for a in rows:
        if not a.predictions:
            continue  # `_empty` — a legitimate outcome for a 2-lap final stint
        d = a.degradation_s_per_lap
        if not PLAUSIBLE_DEG_S_PER_LAP[0] <= abs(d) <= PLAUSIBLE_DEG_S_PER_LAP[1]:
            problems.append(f"car {a.driver_number}: degradation {d:+.3f} s/lap is "
                            f"outside {PLAUSIBLE_DEG_S_PER_LAP}")
        w = a.recommended_pit_window
        if w is None:
            continue
        # The single failure this whole script was written to catch: a window in tyre
        # age rather than race lap numbers reads as a small number early in a stint.
        if w[0] <= (a.tyre_age_laps or 0) and (a.tyre_age_laps or 0) > 5:
            problems.append(f"car {a.driver_number}: window {w} is at or below tyre "
                            f"age {a.tyre_age_laps} — looks like ages, not lap numbers")
        if not 1 <= w[0] <= w[1] <= race_laps + horizon + 5:
            problems.append(f"car {a.driver_number}: window {w} is not a sane pair of "
                            f"lap numbers for a {race_laps}-lap race")

    if degradations:
        med = statistics.median(degradations)
        print(f"median degradation across the grid: {med:.3f} s/lap "
              f"(expect {PLAUSIBLE_MEDIAN_DEG[0]}-{PLAUSIBLE_MEDIAN_DEG[1]})")
        if not PLAUSIBLE_MEDIAN_DEG[0] <= med <= PLAUSIBLE_MEDIAN_DEG[1]:
            problems.append(f"grid median degradation {med:.3f} s/lap is outside "
                            f"{PLAUSIBLE_MEDIAN_DEG}")
    if warm > elapsed:
        problems.append(f"warm sweep ({warm:.1f}s) was slower than cold ({elapsed:.1f}s)"
                        " — the OpenF1 cache or the artifact cache is not working")

    print()
    if problems:
        print(f"FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK — every driver returned, the numbers are in range, and repeats are cheap.")
    print("Now eyeball the windows against the 'actual pit laps' column: a model that "
          "is in range but points at the wrong lap is still wrong.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session-key", type=int, default=9159,
                   help="OpenF1 session key (default: 2023 Singapore GP race)")
    p.add_argument("--laps", type=int, default=62,
                   help="race distance, for the pit-window sanity check")
    p.add_argument("--horizon", type=int, default=10)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.session_key, args.laps, args.horizon)))
