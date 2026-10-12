"""Synthetic race sessions for the Phase 6 tests, stored through the real ParquetRepository."""
from __future__ import annotations

from datetime import datetime, timezone

from app.ingestion.models import DriverInfo, LapRecord, SessionBundle, SessionInfo
from app.storage.repository import ParquetRepository


def race_lap(key: str, driver: str, n: int, stint: int, age: int, t: float | None, **extra) -> LapRecord:
    fields = dict(
        session_key=key, driver=driver, driver_number="1" if driver == "VER" else "4", team="T",
        lap_number=n, lap_time_s=t, sector_s=(None, None, None), stint=stint, compound="MEDIUM" if stint == 1 else "HARD",
        tyre_age=age, fresh_tyre=True, pit_in_time_s=None, pit_out_time_s=None, track_status="1",
        is_accurate=True, deleted=False, deleted_reason=None, position=1, lap_start_session_time_s=None,
        lap_end_session_time_s=None, air_temp_c=24.0, track_temp_c=40.0, rainfall=False,
    )
    fields.update(extra)
    return LapRecord(**fields)


def race_laps(key: str, driver: str = "VER") -> list[LapRecord]:
    """20-lap race: lap 1, stint 1 = laps 1–10 (pit in on 10), stint 2 = laps 11–20 (out lap 11),
    safety car on lap 6, one 6 s outlier on lap 16. Lap time rises 0.05 s per tyre-age lap."""
    laps: list[LapRecord] = []
    for n in range(1, 21):
        stint = 1 if n <= 10 else 2
        age = n if stint == 1 else n - 10
        t = 95.0 + 0.05 * age - 0.03 * n
        extra: dict[str, object] = {}
        if n == 6:
            extra["track_status"] = "14"
        if n == 10:
            extra["pit_in_time_s"] = 1000.0
        if n == 11:
            extra["pit_out_time_s"] = 1025.0
        if n == 16:
            t += 6.0
        laps.append(race_lap(key, driver, n, stint, age, t, **extra))
    return laps


def store_race(repo: ParquetRepository, key: str, *, code: str = "R", laps: list[LapRecord] | None = None,
               total_laps: int | None = 20) -> None:
    year, rnd, _ = key.split("_")
    info = SessionInfo(
        key=key, year=int(year), round=int(rnd), event_name=f"Test GP {rnd}", circuit="Testville",
        country="Nowhere", session_code=code, session_name="Race" if code == "R" else "Qualifying",
        date=datetime(int(year), 5, 1, 13, tzinfo=timezone.utc), n_laps_scheduled=total_laps,
    )
    laps = laps if laps is not None else race_laps(key) + race_laps(key, "NOR")
    repo.save_session(SessionBundle(
        info=info,
        drivers=[DriverInfo(code="VER", number="1", full_name="V", team="T", team_color="#123456"),
                 DriverInfo(code="NOR", number="4", full_name="N", team="T", team_color="#654321")],
        laps=laps, weather=[], corners=[], telemetry={}, fastf1_version="3.5.3",
    ))
