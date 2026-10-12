"""Print the findings report for two drivers' fastest valid laps in a stored session.

    cd backend && python -m scripts.print_findings --key 2024_05_Q --reference VER --target ALO [--json out.json]
    python -m scripts.print_findings --data-dir tests/fixtures/ingestion/store --key 2024_05_Q ...
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import settings
from app.ingestion.service import get_fastest_valid_lap
from app.processing.delta import compare_laps
from app.processing.insights import FindingsReport, build_session_context, generate_findings
from app.processing.models import LapData
from app.storage.repository import ParquetRepository


def build_report(repo: ParquetRepository, key: str, reference: str, target: str) -> FindingsReport:
    ref_rec, tgt_rec = get_fastest_valid_lap(repo, key, reference), get_fastest_valid_lap(repo, key, target)
    cmp = compare_laps(LapData.from_repository(repo, key, ref_rec.driver, ref_rec.lap_number),
                       LapData.from_repository(repo, key, tgt_rec.driver, tgt_rec.lap_number))
    context = build_session_context(ref_rec, tgt_rec, repo.get_session_info(key))
    return generate_findings(cmp, repo.get_corners(key), context=context)


def _fmt(value: object, nd: int = 1) -> str:
    return "-" if value is None else f"{value:+.{nd}f}" if isinstance(value, float) else str(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--key", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--json", help="write the full report JSON here")
    args = parser.parse_args()

    report = build_report(ParquetRepository(args.data_dir), args.key, args.reference, args.target)
    s = report.lap_summary
    print(f"{report.comparison_id}  total {s.time_loss_s:+.3f}s  (official gap {s.metrics['official_gap_s']:+.3f}s)  "
          f"sectors {_fmt(s.metrics['sector_1_delta_s'], 3)} / {_fmt(s.metrics['sector_2_delta_s'], 3)} / "
          f"{_fmt(s.metrics['sector_3_delta_s'], 3)}  top speed {_fmt(s.metrics['top_speed_diff_kph'])} km/h")
    print(f"{'corner':24s} {'span m':>11s} {'type':6s} {'loss':>7s} {'entry':>7s} {'exit':>7s} "
          f"{'dBrk m':>7s} {'dMin':>6s} {'dExit':>6s} {'dFull s':>7s} rank conf    causes")
    for f in report.corners:
        m = f.metrics
        print(f"{f.label:24s} {f.segment.start_m:5.0f}-{f.segment.end_m:<5.0f} {f.corner_type or '-':6s} "
              f"{f.time_loss_s:+7.3f} {f.entry_loss_s:+7.3f} {f.exit_loss_s:+7.3f} {_fmt(m['braking_start_diff_m']):>7s} "
              f"{_fmt(m['min_speed_diff_kph']):>6s} {_fmt(m['exit_speed_diff_kph']):>6s} "
              f"{_fmt(m['time_to_full_throttle_diff_s'], 2):>7s} {f.rank:>4d} {f.confidence:7s} {','.join(f.causes)}")
    for f in report.straights:
        print(f"{f.label:36s} loss {f.time_loss_s:+.3f}  top speed {_fmt(f.metrics['top_speed_diff_kph'])} km/h  "
              f"{','.join(f.causes)}")
    print("largest losses:", report.largest_losses)
    print("largest gains: ", report.largest_gains)
    print("context:", report.session_context.model_dump(exclude={"reference", "target"}))
    for side in ("reference", "target"):
        c = getattr(report.session_context, side)
        print(f"  {side}: {c.driver} lap {c.lap_number} {c.lap_time_s:.3f}s {c.compound} age {c.tyre_age_laps} "
              f"track {c.track_temp_c}C air {c.air_temp_c}C flags {c.track_status_flags}")
    print("notes:", *report.notes, sep="\n  - ")
    if args.json:
        Path(args.json).write_text(json.dumps(report.model_dump(mode="json"), indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
