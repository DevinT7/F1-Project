"""Phase 7D live evaluation: generate reports for ≥ 30 stored comparisons with the REAL LLM.
Never runs in CI: it needs --live (and LLM_PROVIDER / LLM_MODEL / an API key, e.g. in backend/.env).

    cd backend && python -m scripts.eval_report_quality --live [--n 30] [--seed 7]
    cd backend && python -m scripts.eval_report_quality --fake          # the whole pipeline, offline

Comparisons are spread over circuits and driver pairs, qualifying and race, with and without
tyre facts (race targets get the tyre forecast "as of" their lap on every other comparison).
Prints the outcome rates, first-attempt violations by rule, mean latency / tokens, the estimated
cost (when LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK are set), and ten random reports
side by side with the facts they cite, for grading on the 1–5 rubric. Writes everything to
data/ai_eval/<timestamp>/ and, for live runs, appends the summary to docs/progress.md.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import statistics
import sys
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ai.factsheet import FactSheetContext, build_factsheet
from app.ai.fallback import build_template_report
from app.ai.llm_client import FakeLLMClient, LLMClient, ProviderClient, ProviderConfig
from app.ai.prompts import PROMPT_VERSION
from app.ai.report_generator import GenerationResult, Outcome, generate_report
from app.ai.schemas import REPORT_SECTIONS, FactSheet
from app.api.routes.tires import to_schema
from app.config import settings
from app.processing.delta import compare_laps
from app.processing.insights import build_session_context, generate_findings
from app.processing.models import LapData
from app.storage.repository import ParquetRepository
from app.tires.registry import ModelRegistry
from app.tires.service import TireService

REPO = Path(__file__).resolve().parents[2]
RUBRIC = "accuracy, clarity, usefulness, tone (1–5 each)"


@dataclass
class Case:
    key: str
    target: tuple[str, int]
    reference: tuple[str, int]
    tires: bool


def load_env_file() -> None:
    """backend/.env and the repo .env provide LLM_* variables (never printed)."""
    for path in (REPO / "backend" / ".env", REPO / ".env"):
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def choose_cases(repo: ParquetRepository, n: int, rng: random.Random) -> list[Case]:
    sessions = [i for i in repo.list_sessions() if i.session_code in ("R", "Q")]
    rng.shuffle(sessions)
    cases: list[Case] = []
    for info in itertools.cycle(sessions):
        if len(cases) >= n or len(cases) > 4 * n:
            break
        laps = [l for l in repo.list_laps(info.key) if l.is_valid_for_comparison and l.lap_time_s]
        if len(laps) < 2:
            continue
        if info.session_code == "Q":
            best: dict[str, Any] = {}
            for l in laps:
                if l.driver not in best or (l.lap_time_s or 1e9) < (best[l.driver].lap_time_s or 1e9):
                    best[l.driver] = l
            ranked = sorted(best.values(), key=lambda l: l.lap_time_s or 1e9)
            a, b = rng.sample(ranked[:10], 2)
            cases.append(Case(info.key, (a.driver, a.lap_number), (b.driver, b.lap_number), tires=False))
        else:
            lap_no = rng.choice(sorted({l.lap_number for l in laps if l.lap_number > 5}))
            same = [l for l in laps if l.lap_number == lap_no]
            if len(same) < 2:
                continue
            a, b = rng.sample(same, 2)
            cases.append(Case(info.key, (a.driver, a.lap_number), (b.driver, b.lap_number), tires=len(cases) % 2 == 0))
    return cases[:n]


def build(repo: ParquetRepository, tires: TireService, case: Case) -> FactSheet:
    info = repo.get_session_info(case.key)
    recs = {(l.driver, l.lap_number): l for d in (case.target[0], case.reference[0]) for l in repo.list_laps(case.key, d)}
    tgt, ref = recs[case.target], recs[case.reference]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cmp = compare_laps(LapData.from_repository(repo, case.key, *case.reference),
                           LapData.from_repository(repo, case.key, *case.target))
    findings = generate_findings(cmp, repo.get_corners(case.key), context=build_session_context(ref, tgt, info))
    tire_json, reason = None, "no tyre forecast was requested for this comparison"
    if info.session_code != "R":
        reason = "tyre forecasting requires a race session; this is qualifying"
    elif case.tires:
        tire_json = to_schema(tires.predict(case.key, case.target[0], case.target[1], 5)).model_dump(mode="json")
    teams = {d.code: d.team for d in repo.list_drivers(case.key) if d.team}
    return build_factsheet(findings, tire_json, FactSheetContext(session_date=info.date.date().isoformat(), teams=teams,
                                                                 tire_unavailable_reason=None if tire_json else reason))


def side_by_side(fs: FactSheet, result: GenerationResult) -> str:
    facts = {f.id: f for f in fs.facts}
    lines = [f"### {fs.session.key} · {fs.drivers.target.code} (lap {fs.drivers.target.lap}) vs "
             f"{fs.drivers.reference.code} (lap {fs.drivers.reference.lap}) · outcome {result.outcome.value}", ""]
    report = result.report
    for name in ("headline", *REPORT_SECTIONS):
        bullets = [report.headline] if name == "headline" else getattr(report, name)
        for b in bullets:
            cited = "; ".join(f"{i}={facts[i].display[0] if i in facts and facts[i].display else 'unavailable'}"
                              for i in b.fact_ids if i in facts)
            lines.append(f"- **{name}**: {b.text}  \n  _facts:_ {cited or '(categorical only)'}")
    lines += ["", f"Grade ({RUBRIC}): accuracy _ / clarity _ / usefulness _ / tone _", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.eval_report_quality")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="call the real LLM (never in CI)")
    mode.add_argument("--fake", action="store_true", help="offline run of the whole pipeline")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--no-progress", action="store_true", help="do not append to docs/progress.md")
    args = parser.parse_args(argv)
    if args.live and os.environ.get("CI"):
        print("refusing to run the live evaluation in CI", file=sys.stderr)
        return 2
    load_env_file()
    rng = random.Random(args.seed)
    repo = ParquetRepository(args.data_dir)
    tires = TireService(repo, ModelRegistry(settings.tire_registry_path))
    client: LLMClient | None = ProviderClient(ProviderConfig.from_env()) if args.live else None

    rows: list[tuple[Case, FactSheet, GenerationResult]] = []
    for case in choose_cases(repo, args.n, rng):
        try:
            fs = build(repo, tires, case)
        except Exception as exc:  # a lap pair the alignment refuses: skip it, say so
            print(f"skip {case.key} {case.target} vs {case.reference}: {type(exc).__name__}", flush=True)
            continue
        use: LLMClient = client if client is not None else FakeLLMClient([
            {k: v for k, v in build_template_report(fs).model_dump(mode="json").items() if k in ("headline", *REPORT_SECTIONS)}])
        result = generate_report(fs, use)
        rows.append((case, fs, result))
        print(f"{len(rows):>2} {case.key} {case.target[0]} vs {case.reference[0]} tyres={case.tires}: "
              f"{result.outcome.value} ({result.latency_ms} ms, {sum(result.tokens)} tokens)", flush=True)
    if not rows:
        print("no comparisons could be built", file=sys.stderr)
        return 1

    n = len(rows)
    outcomes = Counter(r.outcome for _, _, r in rows)
    first_rules = Counter(v.rule for _, _, r in rows for v in r.first_attempt_violations)
    llm_ok = outcomes[Outcome.LLM_FIRST_TRY] + outcomes[Outcome.LLM_AFTER_RETRY]
    lat = [r.latency_ms for _, _, r in rows]
    tin = [r.tokens[0] for _, _, r in rows]
    tout = [r.tokens[1] for _, _, r in rows]
    price_in, price_out = os.environ.get("LLM_PRICE_IN_PER_MTOK"), os.environ.get("LLM_PRICE_OUT_PER_MTOK")
    cost = (sum(tin) * float(price_in) + sum(tout) * float(price_out)) / 1e6 if price_in and price_out else None
    model = next((a.model for _, _, r in rows for a in r.attempts if a.model), "?")
    summary = [
        f"Live report evaluation · {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · model `{model}` · "
        f"prompt `{PROMPT_VERSION}` · {n} comparisons ({sum(c.tires for c, _, _ in rows)} with tyre facts, "
        f"{sum(fs.session.session_type == 'Q' for _, fs, _ in rows)} qualifying)",
        "",
        "| Outcome | Count | Share |", "|---|---|---|",
        *[f"| {o.value} | {outcomes[o]} | {outcomes[o] / n:.0%} |" for o in Outcome],
        "",
        f"- Valid within one retry: **{llm_ok / n:.0%}** (target ≥ 95%); first try: **{outcomes[Outcome.LLM_FIRST_TRY] / n:.0%}** "
        f"(target ≥ 80%)",
        f"- First-attempt violations by rule: {', '.join(f'{k} {v}' for k, v in first_rules.most_common()) or 'none'}",
        f"- Latency per report: mean {statistics.mean(lat):.0f} ms, max {max(lat)} ms; tokens per report: "
        f"{statistics.mean(tin):.0f} in / {statistics.mean(tout):.0f} out"
        + (f"; estimated cost ${cost:.4f} total (${cost / n:.4f} per report)" if cost is not None else
           "; cost: set LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK to estimate"),
    ]
    print("\n" + "\n".join(summary))
    out = Path(args.data_dir) / "ai_eval" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    sample = rng.sample(rows, min(10, n))
    samples_md = "\n".join(side_by_side(fs, r) for _, fs, r in sample)
    (out / "samples.md").write_text("# Ten sampled reports for grading\n\n" + samples_md, encoding="utf-8")
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    (out / "reports.jsonl").write_text("\n".join(json.dumps({"session": fs.session.key, "outcome": r.outcome.value,
                                                              "first_rules": sorted({v.rule for v in r.first_attempt_violations}),
                                                              "report": r.report.model_dump(mode="json")})
                                                  for _, fs, r in rows) + "\n", encoding="utf-8")
    print(f"\nTen sampled reports (side by side with their facts): {out / 'samples.md'}")
    print(samples_md)
    if args.live and not args.no_progress:
        progress = REPO / "docs" / "progress.md"
        progress.write_text(progress.read_text(encoding="utf-8").rstrip() + "\n\n### 7D live evaluation\n\n"
                            + "\n".join(summary) + f"\n\nSamples for grading: `{out / 'samples.md'}`.\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
