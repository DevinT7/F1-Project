"""Mutation test for the 7B validator: mutate every good corpus case and count how many mutants
the validator rejects (target ≥ 98%).

    cd backend && python -m scripts.validator_mutation_test [--seed 7] [--per-token 3]

Mutations (seeded):
  digit   change one digit of a real number (labels, masked identifiers and number words untouched)
  sign    flip a direction word (lost↔gained, slower↔faster, later↔earlier, less↔more, …)
  unit    change the unit after a number (s↔m, km/h→m, s/lap→km/h, %→s)
Survivors are printed with the case id; docs/validator_rules.md analyses them.
"""
from __future__ import annotations

import argparse
import copy
import random
import re
import sys
from dataclasses import dataclass
from typing import Any

from app.ai.numbers import parse_numbers
from app.ai.validator import _index, validate_report

FLIPS = [("lost", "gained"), ("gained", "lost"), ("gave away", "made up"), ("slower", "faster"), ("faster", "slower"),
         ("behind", "ahead of"), ("later", "earlier"), ("earlier", "later"), ("less", "more"), ("more", "less"),
         ("lower", "higher"), ("higher", "lower"), ("deficit", "advantage"), ("gain", "loss"), ("cost", "saved")]
UNIT_SWAPS = {"s/lap": "km/h", "km/h": "m", "s": "m", "m": "s", "%": " s", "metres": "seconds"}


@dataclass
class Mutant:
    case_id: str
    kind: str
    before: str
    after: str
    killed: bool
    rules: list[str]


def _mutate_digits(text: str, masks: tuple[str, ...], rng: random.Random, per_token: int) -> list[str]:
    out = []
    for tok in parse_numbers(text, ignore=masks):
        if tok.kind == "word":
            continue
        positions = [tok.start + i for i, ch in enumerate(text[tok.start:tok.end]) if ch.isdigit()]
        for pos in rng.sample(positions, min(per_token, len(positions))):
            new = rng.choice([d for d in "0123456789" if d != text[pos]])
            out.append(text[:pos] + new + text[pos + 1:])
    return out


def _mutate_signs(text: str) -> list[str]:
    out = []
    for old, new in FLIPS:
        for m in re.finditer(rf"\b{re.escape(old)}\b", text):
            out.append(text[:m.start()] + new + text[m.end():])
    return out


def _mutate_units(text: str, masks: tuple[str, ...]) -> list[str]:
    out = []
    for tok in parse_numbers(text, ignore=masks):
        if tok.unit is None or tok.kind == "word":
            continue
        tail = text[tok.start:tok.end]
        m = re.search(r"(?<![a-z])(s/lap|km/h|metres|%|s|m)\s*$", tail)
        if not m or m.group(1) not in UNIT_SWAPS:
            continue
        start = tok.start + m.start(1)
        out.append(text[:start] + UNIT_SWAPS[m.group(1)] + text[tok.start + m.end(1):])
    return out


def run(seed: int = 7, per_token: int = 3) -> list[Mutant]:
    from tests.test_ai_validator import GOOD, build  # the corpus harness is the single source of cases

    rng = random.Random(seed)
    mutants: list[Mutant] = []
    for case in GOOD:
        report, fs = build(case)
        masks = _index(fs).masks
        paths: list[tuple[str, int | None]] = [("headline", None)] + [
            (s, i) for s in ("biggest_time_loss", "strongest_areas", "tire_notes", "driving_suggestions",
                             "strategy_options", "data_limitations") for i in range(len(report[s]))]
        if "bullet" in case:  # single-bullet cases: mutate only the case's own bullet
            paths = [(case["section"], None if case["section"] == "headline" else 0)]
        for section, i in paths:
            bullet = report[section] if i is None else report[section][i]
            text = bullet["text"]
            for kind, variants in (("digit", _mutate_digits(text, masks, rng, per_token)), ("sign", _mutate_signs(text)),
                                   ("unit", _mutate_units(text, masks))):
                for new_text in variants:
                    if new_text == text:
                        continue
                    mutated: dict[str, Any] = copy.deepcopy(report)
                    target = mutated[section] if i is None else mutated[section][i]
                    target["text"] = new_text
                    result = validate_report(mutated, fs)
                    mutants.append(Mutant(case["id"], kind, text, new_text, not result.ok, sorted(result.rules())))
    return mutants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--per-token", type=int, default=3)
    args = parser.parse_args(argv)
    mutants = run(args.seed, args.per_token)
    killed = sum(m.killed for m in mutants)
    by_kind: dict[str, list[int]] = {}
    for m in mutants:
        by_kind.setdefault(m.kind, [0, 0])
        by_kind[m.kind][0] += m.killed
        by_kind[m.kind][1] += 1
    print(f"mutants: {len(mutants)}; killed: {killed}; kill rate: {killed / len(mutants):.1%}")
    for kind, (k, n) in sorted(by_kind.items()):
        print(f"  {kind:<6} {k}/{n} = {k / n:.1%}")
    survivors = [m for m in mutants if not m.killed]
    for m in survivors:
        print(f"SURVIVOR {m.case_id} [{m.kind}]: {m.after!r}")
    return 0 if killed / len(mutants) >= 0.98 else 1


if __name__ == "__main__":
    sys.exit(main())
