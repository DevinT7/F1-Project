"""Print the Phase 7C template report for every FactSheet fixture (the human review), and with
--write store them as the snapshot files tests/fixtures/ai/<name>.report.json.

    cd backend && python -m scripts.print_template_reports [--write]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.ai.factsheet import factsheet_from_inputs
from app.ai.fallback import build_template_report
from app.ai.schemas import Report
from app.ai.validator import validate_report

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ai"
SECTIONS = [("biggest_time_loss", "Biggest time loss"), ("strongest_areas", "Strongest areas"),
            ("tire_notes", "Tyre notes"), ("driving_suggestions", "Driving suggestions"),
            ("strategy_options", "Strategy options"), ("data_limitations", "Data limitations")]


def render(report: Report) -> str:
    lines = [f"# {report.headline.text}"]
    for key, title in SECTIONS:
        bullets = getattr(report, key)
        if bullets:
            lines.append(f"\n{title}")
            lines += [f"  • {b.text}" for b in bullets]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="store the snapshots")
    args = parser.parse_args()
    for path in sorted(FIXTURES.glob("*.inputs.json")):
        name = path.name.removesuffix(".inputs.json")
        fs = factsheet_from_inputs(json.loads(path.read_text(encoding="utf-8")))
        report = build_template_report(fs)
        result = validate_report(report, fs)
        words = sum(len(b.text.split()) for b in [report.headline] + [b for k, _ in SECTIONS for b in getattr(report, k)])
        print(f"\n===== {name}  ({'valid' if result.ok else 'INVALID'}, {words} words)")
        print(render(report))
        if args.write:
            (FIXTURES / f"{name}.report.json").write_text(
                json.dumps(report.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
