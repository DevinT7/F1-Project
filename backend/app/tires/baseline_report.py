"""Phase 6B on real races: fit Baseline B and A on every race of the 6A dataset.

    cd backend && python -m app.tires.baseline_report [--data-dir ../data] [--hash <prefix>] [--write]

Nothing here is tuned on real data: thresholds and interval rules were fixed on synthetic truth.
A race that fails is recorded with its error, never skipped silently. --write replaces the
generated block in docs/tire_baselines.md.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import settings
from .models.base import StintContext
from .models.baseline_fixed import BaselineFixed
from .models.baseline_stint import BaselineStint
from .report import latest_outputs

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")
DOC = Path(__file__).resolve().parents[3] / "docs" / "tire_baselines.md"
START, END = "<!-- real:start -->", "<!-- real:end -->"


@dataclass
class RaceResult:
    key: str
    event: str
    laps: int
    beta_fuel: float | None = None
    corr: float | None = None
    deg: dict[str, tuple[float, float, float] | None] = field(default_factory=dict)  # value, lo, hi
    stints_by_compound: dict[str, int] = field(default_factory=dict)
    b_flags: list[str] = field(default_factory=list)
    b_warnings: list[str] = field(default_factory=list)
    a_estimates: int = 0
    a_null: int = 0
    a_flags: Counter[str] = field(default_factory=Counter)
    a_median: dict[str, float | None] = field(default_factory=dict)
    error: str | None = None

    def ordering(self) -> str:
        """SOFT >= MEDIUM >= HARD check on B's estimates, judged with their 95% CIs:
        expected (point order holds), inconclusive (reversed, but the CIs overlap) or
        exception (reversed with non-overlapping CIs: the data really disagree)."""
        present = [c for c in COMPOUNDS if self.deg.get(c) is not None]
        if len(present) < 2:
            return "n/a (one compound)"
        verdict = "expected"
        for softer, harder in zip(present, present[1:]):
            s, h = self.deg[softer], self.deg[harder]
            assert s is not None and h is not None
            if s[0] >= h[0]:
                continue
            if s[2] < h[1]:  # softer's upper bound below harder's lower bound
                return "exception"
            verdict = "inconclusive"
        return verdict

def _ctx(df: pd.DataFrame, compound: str, age: float) -> StintContext | None:
    rows = df[df["compound"] == compound]
    if rows.empty:
        return None
    r = rows.iloc[0]
    return StintContext(str(r["session_key"]), str(r["driver"]), int(r["stint"]), compound, age,
                        df[(df["driver"] == r["driver"]) & (df["stint"] == r["stint"])])


def analyse_race(key: str, df: pd.DataFrame, event: str) -> RaceResult:
    res = RaceResult(key=key, event=event, laps=len(df))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            b = BaselineFixed()
            b.fit(df)
            a = BaselineStint()
            a.fit(df)
        fit = b.fits[key]
        res.beta_fuel, res.corr = fit.beta_fuel, fit.corr_age_fuel
        res.b_flags, res.b_warnings = list(fit.flags), list(fit.warnings)
        res.stints_by_compound = dict(fit.clusters_by_compound)
        for c in COMPOUNDS:
            ctx = _ctx(df, c, 10.0)
            est = b.degradation_per_lap(ctx) if ctx is not None else None
            res.deg[c] = (est.value_s_per_lap, est.ci_low or 0.0, est.ci_high or 0.0) \
                if est is not None and est.value_s_per_lap is not None else None
        for est in a.estimates.values():
            if est.value_s_per_lap is None:
                res.a_null += 1
            else:
                res.a_estimates += 1
            res.a_flags.update(est.flags)
        stint_compound = df.groupby(["driver", "stint"])["compound"].first()
        for c in COMPOUNDS:
            vals = [e.value_s_per_lap for (s, d, st), e in a.estimates.items()
                    if e.value_s_per_lap is not None and stint_compound.get((d, st)) == c]
            res.a_median[c] = float(np.median(vals)) if vals else None
    except Exception as exc:  # recorded, never silent
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def run(frame: pd.DataFrame, events: dict[str, str]) -> list[RaceResult]:
    return [analyse_race(str(k), g, events.get(str(k), str(k))) for k, g in frame.groupby("session_key", sort=True)]


def _deg(v: tuple[float, float, float] | None) -> str:
    return "—" if v is None else f"{v[0]:+.3f} [{v[1]:+.3f}, {v[2]:+.3f}]"


def markdown(results: list[RaceResult], dataset_hash: str) -> str:
    ok = [r for r in results if r.error is None]
    flagged = [r for r in ok if r.b_flags]
    a_flags: Counter[str] = Counter()
    for r in ok:
        a_flags.update(r.a_flags)
    orderings = Counter(r.ordering() for r in ok)
    lines = [
        f"Generated by `python -m app.tires.baseline_report --write` on dataset `{dataset_hash[:12]}` "
        f"({len(results)} races).", "",
        f"- **Ran without crashing:** {len(ok)}/{len(results)} races"
        + ("" if len(ok) == len(results) else " — failures: " + "; ".join(f"{r.key}: {r.error}" for r in results if r.error)),
        f"- **Baseline B flags:** `confounded_fuel_age` in {len(flagged)}/{len(ok)} races"
        + (" (" + ", ".join(r.key for r in flagged) + ")" if flagged else ""),
        f"- **Baseline A:** {sum(r.a_estimates for r in ok)} stint estimates, {sum(r.a_null for r in ok)} null "
        f"(flags: {', '.join(f'{k} {v}' for k, v in a_flags.most_common()) or 'none'})",
        f"- **Compound ordering (B, at tyre age 10):** SOFT ≥ MEDIUM ≥ HARD holds in {orderings['expected']} races; "
        f"reversed but within uncertainty (overlapping 95% CIs) in {orderings['inconclusive']}; "
        f"**clear exception** (reversed, non-overlapping CIs) in {orderings['exception']}",
        "",
        "Baseline B per race: degradation in s/lap at tyre age 10, with 95% CI. β_fuel in s per lap of fuel "
        "(must be > 0). Stints per compound in brackets. A = median of Baseline A per-stint slopes.", "",
        "| Race | Event | Laps | β_fuel | SOFT | MEDIUM | HARD | Order | A median S / M / H | Flags |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.error:
            lines.append(f"| {r.key} | {r.event} | {r.laps} | ERROR | | | | | | {r.error} |")
            continue
        n = r.stints_by_compound

        def cell(c: str) -> str:
            return _deg(r.deg.get(c)) + (f" ({n[c]})" if c in n else "")

        amed = " / ".join("—" if r.a_median.get(c) is None else f"{r.a_median[c]:+.3f}" for c in COMPOUNDS)
        lines.append(f"| {r.key} | {r.event} | {r.laps} | {r.beta_fuel:+.4f} | {cell('SOFT')} | {cell('MEDIUM')} | "
                     f"{cell('HARD')} | {r.ordering()} | {amed} | {', '.join(r.b_flags) or '—'} |")
    lines += [""]
    if flagged:
        lines += ["Warnings from flagged fits:", ""] + [f"- {r.key}: " + "; ".join(r.b_warnings) for r in flagged] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tires.baseline_report")
    parser.add_argument("--data-dir", default=settings.data_dir)
    parser.add_argument("--hash", default=None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    frame, log, meta = latest_outputs(Path(args.data_dir) / "tires", args.hash)
    events = {k: v["event"] for k, v in log["per_race"].items()}
    results = run(frame, events)
    text = markdown(results, meta["hash"])
    print(text)
    if args.write:
        doc = DOC.read_text(encoding="utf-8")
        if START not in doc:
            raise SystemExit(f"{DOC} has no {START} block")
        head, rest = doc.split(START, 1)
        DOC.write_text(f"{head}{START}\n{text}\n{END}{rest.split(END, 1)[1]}", encoding="utf-8")
        print(f"\nwrote {DOC}")
    return 0 if all(r.error is None for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
