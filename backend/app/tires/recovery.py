"""Phase 6B parameter-recovery experiment on synthetic races with known degradation.

    cd backend && python -m app.tires.recovery [--seeds 50] [--write]

For each scenario × seed: generate a race (app.tires.synthetic), fit Baseline B (pooled fixed
effects) and Baseline A (per-stint, fuel-adjusted from B), and compare the degradation estimates
with the truth. Reports mean estimate, bias, SD and empirical 95% CI coverage per compound, the
recovered fuel slope, and how often the confounded_fuel_age flag fires. --write replaces the
generated block in docs/tire_baselines.md. Seeds are SEED + i, so the run is reproducible.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pandas as pd

from .config import SEED
from .models.base import StintContext
from .models.baseline_fixed import BaselineFixed
from .models.baseline_stint import BaselineStint
from .synthetic import TrueParams, aligned_plans, make_race

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")
DOC = Path(__file__).resolve().parents[3] / "docs" / "tire_baselines.md"
START, END = "<!-- recovery:start -->", "<!-- recovery:end -->"


@dataclass(frozen=True)
class Scenario:
    name: str
    noise_sd: float = 0.15
    params: TrueParams = field(default_factory=TrueParams)
    aligned: bool = False


SCENARIOS = (
    Scenario("default (noise 0.15 s, staggered stops)"),
    Scenario("noise-free", noise_sd=0.0),
    Scenario("heavy noise (0.6 s)", noise_sd=0.6),
    Scenario("hidden track evolution (−0.02 s/lap)", params=TrueParams(track_evo_per_lap=-0.02)),
    Scenario("collinear: no staggered stops", aligned=True),
)


def _ctx(df: pd.DataFrame, compound: str) -> StintContext | None:
    rows = df[df["compound"] == compound]
    if rows.empty:
        return None
    row = rows.iloc[0]
    stint = df[(df["driver"] == row["driver"]) & (df["stint"] == row["stint"])]
    return StintContext(str(row["session_key"]), str(row["driver"]), int(row["stint"]), compound, 10.0, stint)


def run(scenario: Scenario, seeds: int) -> dict[str, object]:
    truth = scenario.params.deg_linear
    rec: dict[str, dict[str, list[float]]] = {m: {c: [] for c in COMPOUNDS} for m in ("B", "A")}
    cov: dict[str, dict[str, list[bool]]] = {m: {c: [] for c in COMPOUNDS} for m in ("B", "A")}
    fuels: list[float] = []
    flagged = 0
    for i in range(seeds):
        plans = aligned_plans(20, 57) if scenario.aligned else None
        df = make_race(seed=SEED + i, noise_sd=scenario.noise_sd, params=scenario.params, plans=plans)
        b, a = BaselineFixed(), BaselineStint()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            b.fit(df)
            a.fit(df)
        fit = next(iter(b.fits.values()))
        fuels.append(fit.beta_fuel)
        flagged += "confounded_fuel_age" in fit.flags
        for c in COMPOUNDS:
            ctx = _ctx(df, c)
            if ctx is None:
                continue
            for name, model in (("B", b), ("A", a)):
                est = model.degradation_per_lap(ctx)
                if est.value_s_per_lap is None:
                    continue
                rec[name][c].append(est.value_s_per_lap)
                if est.ci_low is not None and est.ci_high is not None:
                    cov[name][c].append(est.ci_low <= truth[c] <= est.ci_high)
    return {"rec": rec, "cov": cov, "fuels": fuels, "flagged": flagged, "seeds": seeds}


def table(scenario: Scenario, out: dict[str, object]) -> list[str]:
    rec = out["rec"]
    cov = out["cov"]
    fuels = np.asarray(out["fuels"], dtype=float)
    truth = scenario.params.deg_linear
    assert isinstance(rec, dict) and isinstance(cov, dict)
    lines = [f"**{scenario.name}** · {out['seeds']} seeds · `confounded_fuel_age` flagged in "
             f"{out['flagged']}/{out['seeds']} fits · recovered β_fuel mean {np.nanmean(fuels):+.4f} "
             f"(true {scenario.params.fuel_slope:+.4f}"
             + (f"; with track evolution the identifiable value is {scenario.params.fuel_slope - scenario.params.track_evo_per_lap:+.4f}"
                if scenario.params.track_evo_per_lap else "") + ")", "",
             "| Compound | True (s/lap) | B mean | B bias | B SD | B 95% coverage | A mean | A bias | A 95% coverage |",
             "|---|---|---|---|---|---|---|---|---|"]
    def mean(x: npt.NDArray[np.float64]) -> str:
        return f"{x.mean():.4f}" if x.size else "—"

    def bias(x: npt.NDArray[np.float64], t: float) -> str:
        return f"{x.mean() - t:+.4f}" if x.size else "—"

    def coverage(x: npt.NDArray[np.float64]) -> str:
        if scenario.noise_sd == 0:
            return "n/a (σ = 0)"  # zero-width intervals: "coverage" would only measure float round-off
        return f"{x.mean():.0%}" if x.size else "—"

    for c in COMPOUNDS:
        b, a = np.asarray(rec["B"][c], dtype=float), np.asarray(rec["A"][c], dtype=float)
        bc, ac = np.asarray(cov["B"][c], dtype=float), np.asarray(cov["A"][c], dtype=float)
        if not b.size and not a.size:
            lines.append(f"| {c} | {truth[c]:.3f} | not run in this scenario | | | | | | |")
            continue
        lines.append(f"| {c} | {truth[c]:.3f} | {mean(b)} | {bias(b, truth[c])} | {b.std():.4f} | {coverage(bc)} | "
                     f"{mean(a)} | {bias(a, truth[c])} | {coverage(ac)} |")
    return lines + [""]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.tires.recovery")
    parser.add_argument("--seeds", type=int, default=50)
    parser.add_argument("--write", action="store_true", help="update docs/tire_baselines.md")
    args = parser.parse_args(argv)
    lines = [f"Generated by `python -m app.tires.recovery --seeds {args.seeds} --write` (seeds {SEED}…{SEED + args.seeds - 1}).",
             "Truth: SOFT 0.12, MEDIUM 0.08, HARD 0.05 s/lap linear; fuel 0.06 s per lap of fuel; 20 drivers × 57 laps.", ""]
    for scenario in SCENARIOS:
        lines += table(scenario, run(scenario, args.seeds))
    text = "\n".join(lines)
    print(text)
    if args.write:
        doc = DOC.read_text(encoding="utf-8") if DOC.exists() else f"# Tire baselines (Phase 6B)\n\n{START}\n{END}\n"
        head, rest = doc.split(START, 1)
        DOC.write_text(f"{head}{START}\n{text}\n{END}{rest.split(END, 1)[1]}", encoding="utf-8")
        print(f"\nwrote {DOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
