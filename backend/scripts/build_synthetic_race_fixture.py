"""Build the synthetic race session used by the Phase 6F tyre-panel tests and screenshots.

    cd backend && python -m scripts.build_synthetic_race_fixture

Writes tests/fixtures/ingestion/store/2024_99_R ("RaceIQ Synthetic Grand Prix", 50 laps; round
99 does not exist, so it never collides with real data). Deterministic (seeded). Laps follow a
plain fuel + wear model, and three drivers each exercise one panel state:

  AAA  MEDIUM 1–22, HARD 23–50. From lap 41 every lap is DISTINCTIVE: about 4 s slower, a step
       nothing earlier predicts. An "as of lap N ≤ 40" view must never show it (the cut rule).
  BBB  SOFT 1–15, MEDIUM 16–46, then SOFT 47–50: four laps, the out-lap excluded, so 3 clean
       laps at the flag → insufficient_data, "3/5 clean laps".
  CCC  INTERMEDIATE 1–8 in the rain (rainfall on every lap), MEDIUM 9–50 → the wet notice.

No lap is valid for telemetry comparison (there is no telemetry), so the dashboard never
starts a comparison for this session.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.ingestion.models import DriverInfo, LapRecord, SessionBundle, SessionInfo
from app.storage.repository import ParquetRepository

STORE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ingestion" / "store"
KEY = "2024_99_R"
TOTAL = 50
DISTINCTIVE_FROM_LAP = 41
DISTINCTIVE_STEP_S = 4.0
DEG = {"SOFT": 0.09, "MEDIUM": 0.06, "HARD": 0.04, "INTERMEDIATE": 0.03}
OFFSET = {"SOFT": -0.6, "MEDIUM": 0.0, "HARD": 0.4, "INTERMEDIATE": 9.0}
FUEL_S_PER_LAP = 0.055
DRIVERS = {  # code: (number, name, team, colour, base pace, stints [(first, last, compound)])
    "AAA": ("91", "Ada Alvarez", "Synthetic Racing", "#3671C6", 90.0, [(1, 22, "MEDIUM"), (23, 50, "HARD")]),
    "BBB": ("92", "Ben Brooks", "Synthetic Racing", "#3671C6", 90.3,
            [(1, 15, "SOFT"), (16, 46, "MEDIUM"), (47, 50, "SOFT")]),
    "CCC": ("93", "Cai Chen", "Model Motorsport", "#E8002D", 90.5, [(1, 8, "INTERMEDIATE"), (9, 50, "MEDIUM")]),
}


def laps_for(code: str, rng: np.random.Generator) -> list[LapRecord]:
    number, _name, team, _colour, base, stints = DRIVERS[code]
    out = []
    for stint_no, (first, last, compound) in enumerate(stints, start=1):
        for n in range(first, last + 1):
            age = n - first + 1
            t = base + OFFSET[compound] + DEG[compound] * age + FUEL_S_PER_LAP * (TOTAL - n) + rng.normal(0, 0.12)
            if n == 1:
                t += 6.0                        # standing start
            if n == first and stint_no > 1:
                t += 20.0                       # out-lap
            if n == last and n != TOTAL:
                t += 18.0                       # in-lap
            if code == "AAA" and n >= DISTINCTIVE_FROM_LAP:
                t += DISTINCTIVE_STEP_S         # the distinctive later laps
            out.append(LapRecord(
                session_key=KEY, driver=code, driver_number=number, team=team, lap_number=n, lap_time_s=round(t, 3),
                sector_s=(None, None, None), stint=stint_no, compound=compound, tyre_age=age, fresh_tyre=True,
                pit_in_time_s=float(1000 + n * 95) if n == last and n != TOTAL else None,
                pit_out_time_s=float(990 + n * 95) if n == first and stint_no > 1 else None,
                track_status="1", is_accurate=True, deleted=False, deleted_reason=None, position=None,
                lap_start_session_time_s=None, lap_end_session_time_s=None,
                air_temp_c=24.0, track_temp_c=38.0, rainfall=compound == "INTERMEDIATE",
                is_valid_for_comparison=False, invalid_reason="no telemetry (synthetic fixture)",
            ))
    return out


def bundle() -> SessionBundle:
    rng = np.random.default_rng(6)
    info = SessionInfo(key=KEY, year=2024, round=99, event_name="RaceIQ Synthetic Grand Prix", circuit="Testville",
                       country="Nowhere", session_code="R", session_name="Race",
                       date=datetime(2024, 12, 31, 14, 0, tzinfo=timezone.utc), n_laps_scheduled=TOTAL,
                       event_format="conventional")
    drivers = [DriverInfo(code=c, number=v[0], full_name=v[1], team=v[2], team_color=v[3], position=i + 1)
               for i, (c, v) in enumerate(DRIVERS.items())]
    laps = [lap for code in DRIVERS for lap in laps_for(code, rng)]
    return SessionBundle(info=info, drivers=drivers, laps=laps, weather=[], corners=[], fastf1_version="synthetic")


def main() -> None:
    ParquetRepository(STORE).save_session(bundle())
    print(f"wrote {STORE / KEY}")


if __name__ == "__main__":
    main()
