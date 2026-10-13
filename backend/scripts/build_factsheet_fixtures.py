"""Build the Phase 7A FactSheet fixtures from real ingested sessions.

    cd backend && python -m scripts.build_factsheet_fixtures [--data-dir ../data]

For each case: compare the two laps (Phase 2/3 → findings JSON), get the tyre response for the
target "as of" its lap (Phase 6E) or record why there is none, then write
tests/fixtures/ai/<name>.inputs.json (findings + tire + context) and <name>.factsheet.json (the
builder's output, a golden file). The inputs are committed, so the tests need no data directory.
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path
from typing import Any

from app.api.routes.tires import to_schema
from app.config import settings
from app.processing.delta import compare_laps
from app.processing.insights import build_session_context, generate_findings
from app.processing.models import LapData
from app.storage.repository import ParquetRepository
from app.tires.registry import ModelRegistry
from app.tires.service import TireService

from app.ai.factsheet import factsheet_from_inputs

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ai"
NON_RACE = "tyre forecasting requires a race session; this is qualifying"
MODEL_503 = "tyre model unavailable (HTTP 503: model_not_available)"

# name, session, (target driver, lap), (reference driver, lap), tyre: "predict" | reason string
CASES: list[tuple[str, str, tuple[str, int], tuple[str, int], str]] = [
    ("q_alo_vs_ver_fastest", "2024_05_Q", ("ALO", 0), ("VER", 0), NON_RACE),
    ("q_bot_vs_alo_no_braking", "2024_05_Q", ("BOT", 2), ("ALO", 2), NON_RACE),
    ("race_lec_pit_window", "2024_14_R", ("LEC", 39), ("PIA", 39), "predict"),
    ("race_alo_tire_model_unavailable", "2024_14_R", ("ALO", 2), ("ALB", 2), MODEL_503),
    ("race_lec_tire_insufficient", "2024_14_R", ("LEC", 28), ("PIA", 28), "predict"),
]


def fastest(repo: ParquetRepository, key: str, driver: str) -> int:
    valid = [l for l in repo.list_laps(key, driver) if l.is_valid_for_comparison and l.lap_time_s]
    return min(valid, key=lambda l: l.lap_time_s or 1e9).lap_number


def nearest_valid(repo: ParquetRepository, key: str, driver: str, lap: int) -> int:
    valid = sorted(l.lap_number for l in repo.list_laps(key, driver) if l.is_valid_for_comparison)
    return min(valid, key=lambda n: (abs(n - lap), n))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=settings.data_dir)
    args = parser.parse_args()
    repo = ParquetRepository(args.data_dir)
    tires = TireService(repo, ModelRegistry(settings.tire_registry_path))
    OUT.mkdir(parents=True, exist_ok=True)
    for name, key, (tdrv, tlap), (rdrv, rlap), tyre in CASES:
        tlap = fastest(repo, key, tdrv) if tlap == 0 else nearest_valid(repo, key, tdrv, tlap)
        rlap = fastest(repo, key, rdrv) if rlap == 0 else nearest_valid(repo, key, rdrv, rlap)
        recs = {(l.driver, l.lap_number): l for d in (tdrv, rdrv) for l in repo.list_laps(key, d)}
        ref, tgt = recs[(rdrv, rlap)], recs[(tdrv, tlap)]
        info = repo.get_session_info(key)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cmp = compare_laps(LapData.from_repository(repo, key, rdrv, rlap), LapData.from_repository(repo, key, tdrv, tlap))
        findings = generate_findings(cmp, repo.get_corners(key), context=build_session_context(ref, tgt, info))
        tire: dict[str, Any] | None = None
        reason: str | None = None
        if tyre == "predict":
            tire = to_schema(tires.predict(key, tdrv, tlap, 5)).model_dump(mode="json")
        else:
            reason = tyre
        teams = {d.code: d.team for d in repo.list_drivers(key) if d.team}
        inputs = {"findings": findings.model_dump(mode="json"), "tire": tire,
                  "context": {"session_date": info.date.date().isoformat(), "teams": teams,
                              "tire_unavailable_reason": reason}}
        (OUT / f"{name}.inputs.json").write_text(json.dumps(inputs, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        fs = factsheet_from_inputs(inputs)
        (OUT / f"{name}.factsheet.json").write_text(json.dumps(fs.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n",
                                                    encoding="utf-8")
        tstatus = fs.tire.get("status")
        print(f"{name}: {tdrv} L{tlap} vs {rdrv} L{rlap}; {len(fs.facts)} facts, {len(fs.categorical_facts)} categorical; "
              f"tyre {tstatus}; hash {fs.factsheet_hash[:12]}")


if __name__ == "__main__":
    main()
