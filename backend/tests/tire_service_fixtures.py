"""In-memory race repository and a tiny registry for the Phase 6E service tests."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.ingestion.models import DriverInfo, LapRecord, SessionInfo
from app.tires.registry import RegistryEntry, RegistryFile, sha256_file
from app.tires.serving import FORMAT, FixedEffectsPoint, PersistencePoint, PointModel, point_model_to_json, residuals_to_npz

RACE, QUALI = "2024_05_R", "2024_05_Q"
TOTAL_LAPS = 40
# AAA: MEDIUM laps 1–18 (pits end of 18), HARD 19–40. BBB: HARD all race.
STINTS = {"AAA": [(1, 1, 18, "MEDIUM"), (2, 19, 40, "HARD")], "BBB": [(1, 1, 40, "HARD")]}


def lap_records(seed: int = 0) -> list[LapRecord]:
    rng = np.random.default_rng(seed)
    out = []
    for i, (code, stints) in enumerate(STINTS.items()):
        for stint, first, last, compound in stints:
            for n in range(first, last + 1):
                age = n - first + 1
                t = 90.0 + 0.6 * i + 0.06 * age + 0.05 * (TOTAL_LAPS - n) + rng.normal(0, 0.08)
                out.append(LapRecord(
                    session_key=RACE, driver=code, driver_number=str(10 + i), team="Team", lap_number=n,
                    lap_time_s=round(t, 3), sector_s=(None, None, None), stint=stint, compound=compound, tyre_age=age,
                    fresh_tyre=True, pit_in_time_s=1000.0 + n if n == last and n != TOTAL_LAPS else None,
                    pit_out_time_s=900.0 + n if n == first and stint > 1 else None, track_status="1",
                    is_accurate=True, deleted=False, deleted_reason=None, position=i + 1,
                    lap_start_session_time_s=None, lap_end_session_time_s=None, air_temp_c=25.0, track_temp_c=40.0,
                    rainfall=False))
    return out


@dataclass
class FakeRepo:
    laps: list[LapRecord] = field(default_factory=lap_records)

    def has_session(self, key: str) -> bool:
        return key in (RACE, QUALI)

    def get_session_info(self, key: str) -> SessionInfo:
        code = key.rsplit("_", 1)[1]
        return SessionInfo(key=key, year=2024, round=5, event_name="Test GP", circuit="Testville", country="X",
                           session_code=code, session_name="Race" if code == "R" else "Qualifying",
                           date=datetime(2024, 5, 5, tzinfo=timezone.utc),
                           n_laps_scheduled=TOTAL_LAPS if code == "R" else None)

    def list_drivers(self, key: str) -> list[DriverInfo]:
        return [DriverInfo(code=c, number=str(10 + i), full_name=None, team="Team", team_color=None)
                for i, c in enumerate(STINTS)]

    def list_laps(self, key: str, driver: str | None = None) -> list[LapRecord]:
        return [lap for lap in self.laps if driver is None or lap.driver == driver]


def fixed_effects() -> FixedEffectsPoint:
    return FixedEffectsPoint(beta_fuel=0.05, beta_age={"SOFT": 0.09, "MEDIUM": 0.06, "HARD": 0.04},
                             gamma={"SOFT": 0.0, "MEDIUM": 0.0, "HARD": 0.0}, beta_temp=0.0,
                             degradation_ci={"SOFT": (0.07, 0.11), "MEDIUM": (0.05, 0.07), "HARD": (0.03, 0.05)})


def write_registry(root: Path, *, point: PointModel | None = None, spread: float = 0.1, steps: int = 25,
                   status: str = "production") -> Path:
    """Registry with one model; residuals |N(0, spread)|-like per step (seeded)."""
    point = point or fixed_effects()
    rng = np.random.default_rng(1)
    residuals = {s: np.abs(rng.normal(0, spread * (1 + s / 25), 200)) for s in range(1, steps + 1)}
    d = root / "tires" / "m" / "v1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.json").write_text(json.dumps({"format": FORMAT, "point_model": point_model_to_json(point)}), encoding="utf-8")
    (d / "residuals.npz").write_bytes(residuals_to_npz(residuals))
    entry = RegistryEntry(name="test_model", version="v1", kind=point.kind, status=status,  # type: ignore[arg-type]
                          artifact="tires/m/v1/model.json", sha256=sha256_file(d / "model.json"),
                          calibration_residuals="tires/m/v1/residuals.npz",
                          calibration_sha256=sha256_file(d / "residuals.npz"), dataset_hash="d" * 64, git_sha="g" * 40,
                          trained_through="2024-04-21", trained_through_session="2024_04_R", alpha=0.2,
                          metrics={"holdout_h5_mae": 0.5})
    path = root / "registry.json"
    path.write_text(RegistryFile(models=[entry]).model_dump_json(indent=2), encoding="utf-8")
    return path


__all__ = ["RACE", "QUALI", "TOTAL_LAPS", "FakeRepo", "PersistencePoint", "fixed_effects", "lap_records",
           "write_registry"]
