"""Phase 7B: the deterministic guardrail for generated reports (and, in 7E, answers).

    validate_report(report, factsheet) -> ValidationResult(ok, violations)
    explain_for_retry(violations) -> str        # concise feedback for the 7D retry prompt

Rules (docs/validator_rules.md), each producing typed Violations:
  1 schema                    structure matches schemas.Report; no extra keys; fact_ids are strings;
                              the report's factsheet_hash is this FactSheet's
  2 unknown_fact_id           every cited id exists
  3 ungrounded_number         every number matches a display string of a fact cited IN THE SAME
                              BULLET (allow-list: the context lap numbers, the 80%/95% interval levels
                              of cited interval facts, allowed number words)
  3b ungrounded_label         every corner/sector/straight named exists and is cited in the bullet
  4 unit_mismatch             a number with a unit matches a cited fact only under another unit
  5 direction_mismatch        direction words / explicit signs contradict the cited facts' signs
  6 unsupported_causal_claim  causal language must name a cause listed for the cited segment
  7 unavailable_data          a cited null fact gets no number and an explicit availability marker
  8 out_of_scope              injuries, politics, penalties, driver intent, strategy guarantees;
                              strategy options must be hedged
  9 style                     ≤ 60 words per bullet, ≤ 450 per report, digits (no spelled numbers)
 10 prompt_injection          control phrases and URLs
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from .numbers import NumberToken, parse_numbers
from .schemas import Bullet, CategoricalFact, Fact, FactSheet, Report

RULES = ("schema", "unknown_fact_id", "ungrounded_number", "ungrounded_label", "unit_mismatch", "direction_mismatch",
         "unsupported_causal_claim", "unavailable_data", "out_of_scope", "style", "prompt_injection")
SECTIONS = ("headline", "biggest_time_loss", "strongest_areas", "tire_notes", "driving_suggestions",
            "strategy_options", "data_limitations")
MAX_BULLET_WORDS = 60
MAX_REPORT_WORDS = 450
LEXICON_PATH = Path(__file__).with_name("direction_lexicon.yaml")


@dataclass(frozen=True)
class Violation:
    rule: str
    section: str | None
    bullet_index: int | None
    detail: str

    @property
    def where(self) -> str:
        if self.section is None:
            return "report"
        return self.section if self.bullet_index is None else f"{self.section}[{self.bullet_index}]"


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def rules(self) -> set[str]:
        return {v.rule for v in self.violations}


# ---------------------------------------------------------------- lexicons
@dataclass(frozen=True)
class DirectionEntry:
    pattern: re.Pattern[str]
    family: str
    sign: int
    source: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Lexicon:
    families: dict[str, re.Pattern[str]]
    entries: tuple[DirectionEntry, ...]
    cause_groups: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...]   # ordered: specific first
    unsupported_causes: re.Pattern[str]


@lru_cache(maxsize=1)
def lexicon() -> Lexicon:
    raw = yaml.safe_load(LEXICON_PATH.read_text(encoding="utf-8"))
    gap = raw["gap"]
    entries = tuple(
        DirectionEntry(re.compile(e["pattern"].replace("{gap}", gap), re.IGNORECASE), e["family"], int(e["sign"]),
                       e["pattern"], tuple(e["examples"]))
        for e in raw["entries"])
    return Lexicon(
        families={k: re.compile(v) for k, v in raw["families"].items()},
        entries=entries,
        cause_groups=tuple((g["name"], re.compile(g["pattern"], re.IGNORECASE), frozenset(g["causes"]))
                           for g in raw["causes"]),
        unsupported_causes=re.compile(raw["unsupported"], re.IGNORECASE),
    )


CAUSAL_MARKER = re.compile(
    r"\b(?:because(?: of)?|due to|caused by|as a result of|thanks to|owing to|stem(?:s|med|ming)? from|"
    r"attributable to|attributed to|explained by|explains?|led to|leads to|resulting from|down to|the reason|"
    r"contributing factor|related to|linked to)\b", re.IGNORECASE)
DIRECTION_NEGATION = re.compile(r"\b(?:not|never|didn't|did not|wasn't|was not|no longer)$", re.IGNORECASE)
OBJECT_PREPOSITION = re.compile(r"\b(?:on|to|than|over|against|behind|from|versus|vs\.?|with|of|for|ahead of)\s+$",
                                re.IGNORECASE)
COUNT_FACT = re.compile(r"\.(corners_lost|corners_gained)$")
HEDGE_BEFORE = re.compile(r"\b(?:may|might|could|possibly|likely|perhaps|probably)\b", re.IGNORECASE)
NEGATION_BEFORE = re.compile(r"\b(?:not|no|never|cannot|can't|isn't|aren't|wasn't)\b", re.IGNORECASE)
UNAVAILABLE_MARKER = re.compile(
    r"\b(?:unavailable|not available|n/a|not detected|no braking|did not brake|didn't brake|not recorded|no data|"
    r"missing|open-ended|open ended|beyond the (?:forecast )?horizon|no crossing|not applicable|no (?:tyre |tire )?forecast|"
    r"cannot be compared|can't be compared|not reached|never reached?|not measured|unknown|no (?:pit )?window)\b",
    re.IGNORECASE)
SCOPE: dict[str, re.Pattern[str]] = {
    "injury": re.compile(r"\b(?:injur\w*|hurt|pain|wrist|medical|hospital|unwell|illness)\b", re.IGNORECASE),
    "politics": re.compile(r"\b(?:team orders?|politic\w*|contracts?|sacked|fired|favou?rit\w*|number one driver|feud)\b",
                           re.IGNORECASE),
    "rules": re.compile(r"\b(?:penalt\w*|stewards?|disqualif\w*|illegal|cheat\w*|breach\w*|infringement|reprimand\w*|"
                        r"investigat\w*)\b", re.IGNORECASE),
    "intent": re.compile(r"\b(?:scared|afraid|nervous|frustrat\w*|angry|panick\w*|lazy|tired|distracted|wanted to|"
                         r"tried to|deliberately|on purpose|intended to|was thinking|felt)\b", re.IGNORECASE),
    "guarantee": re.compile(r"\b(?:must pit|must stop|you must|has to pit|needs? to pit|pit now|box now|guarantee\w*|"
                            r"definitely|certainly|will win|always pit|the only option|no doubt)\b", re.IGNORECASE),
}
STRATEGY_HEDGE = re.compile(r"\b(?:indicative|suggests?|suggestion|may|might|could|consider\w*|possible|possibly|"
                            r"heuristic|likely|potential\w*)\b", re.IGNORECASE)
INJECTION = re.compile(
    r"\b(?:ignore (?:all |the )?(?:previous|prior|above|earlier) (?:instructions?|rules?|prompts?)|system prompt|"
    r"disregard (?:the|all|previous|prior)|you are now|as an ai|developer mode|jailbreak)\b|https?://|www\.|"
    r"\b[a-z0-9-]+\.(?:com|org|net|io|ai|dev|co|uk)\b", re.IGNORECASE)
LABEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bturns?\s+(\d{1,2})\b", re.IGNORECASE), "T{}"),
    (re.compile(r"\bT(\d{1,2})\b"), "T{}"),
    (re.compile(r"\bsectors?\s+([1-3])\b", re.IGNORECASE), "SEC{}"),
    (re.compile(r"\bS([1-3])\b"), "SEC{}"),
    (re.compile(r"\bstraights?\s+(\d{1,2})\b", re.IGNORECASE), "STR{}"),
    (re.compile(r"\bSTR(\d{1,2})\b"), "STR{}"),
]
CONTEXT_LAPS = ("target.lap_number", "reference.lap_number", "tire.as_of_lap")
INTERVAL_LEVELS = {"tire.deg_ci_low": "95", "tire.deg_ci_high": "95", "tire.next_lap_lo_s": "80",
                   "tire.next_lap_hi_s": "80"}
UNIT_COMPAT: dict[str, frozenset[str]] = {
    "s": frozenset({"s"}), "s/lap": frozenset({"s/lap"}), "km/h": frozenset({"km/h"}), "m": frozenset({"m"}),
    "°C": frozenset({"°C"}), "m/s²": frozenset({"m/s²"}), "%": frozenset({"%", "fraction"}),
    "lap": frozenset({"lap", "laps", "count"}), "laps": frozenset({"lap", "laps", "count"}),
}
SEGMENT_PREFIX = re.compile(r"^(LAP|T\d+|STR\d+|SEC\d+)$")


# ---------------------------------------------------------------- the FactSheet index
@dataclass
class _Index:
    facts: dict[str, Fact]
    categorical: dict[str, CategoricalFact]
    prefixes: set[str]
    causes: dict[str, frozenset[str]]
    masks: tuple[str, ...]
    context_laps: set[int]
    factsheet_hash: str
    subjects: tuple[tuple[re.Pattern[str], str], ...] = ()   # (driver code / team, "target" | "reference")


_INDEX_CACHE: dict[str, _Index] = {}


def _index(fs: FactSheet) -> _Index:
    hit = _INDEX_CACHE.get(fs.factsheet_hash)
    if hit is not None:
        return hit
    facts = {f.id: f for f in fs.facts}
    categorical = {c.id: c for c in fs.categorical_facts}
    prefixes = {i.split(".")[0] for i in facts if SEGMENT_PREFIX.match(i.split(".")[0])}
    causes = {cid.split(".")[0]: frozenset(c.value) - {"unclear"} for cid, c in categorical.items()
              if cid.endswith(".causes") and isinstance(c.value, list)}
    masks: list[str] = [str(c.value) for cid, c in categorical.items()
                        if isinstance(c.value, str) and any(ch.isdigit() for ch in c.value)
                        and (cid.endswith((".label", ".key", ".event", ".session_key", "model_version", "model_name",
                                           "model_trained_through")))]
    masks += [s for s in (fs.session.key, fs.session.event, fs.session.date) if s]
    masks.sort(key=len, reverse=True)  # longest first, so a label is masked before any substring
    laps = {int(v) for i in CONTEXT_LAPS if i in facts and (v := facts[i].value) is not None}
    subjects: list[tuple[re.Pattern[str], str]] = []
    for side, drv in (("target", fs.drivers.target), ("reference", fs.drivers.reference)):
        subjects.append((re.compile(rf"\b{re.escape(drv.code)}\b"), side))      # codes: case-sensitive
    teams = (fs.drivers.target.team, fs.drivers.reference.team)
    if all(teams) and teams[0] != teams[1]:                                       # teams only if they tell them apart
        subjects += [(re.compile(rf"\b{re.escape(str(t))}\b", re.IGNORECASE), side)
                     for t, side in zip(teams, ("target", "reference"), strict=True)]
    idx = _Index(facts, categorical, prefixes, causes, tuple(masks), laps, fs.factsheet_hash, tuple(subjects))
    if len(_INDEX_CACHE) > 256:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[fs.factsheet_hash] = idx
    return idx


# ---------------------------------------------------------------- per-bullet checks
def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def _matches(tok: NumberToken, fact: Fact) -> bool:
    """Do the token's digits equal one of the fact's display forms (or the % form of a fraction)?"""
    if fact.value is None:
        return False
    mag = tok.magnitude_text
    if tok.kind == "laptime":
        mag = f"{abs(tok.value):.{_decimals(tok.magnitude_text)}f}"
    if tok.unit == "%" and fact.unit == "fraction":
        return f"{abs(float(fact.value)) * 100:.{_decimals(mag)}f}" == mag
    return mag in fact.display


def _unit_ok(tok: NumberToken, fact: Fact) -> bool:
    if tok.unit is None:
        return True
    return fact.unit in UNIT_COMPAT.get(tok.unit, frozenset({tok.unit}))


def _sentence_bounds(text: str, pos: int) -> tuple[int, int]:
    ends = [m.end() for m in re.finditer(r"[;!?]|\.(?!\d)", text)]
    start = max((e for e in ends if e <= pos), default=0)
    end = min((e for e in ends if e > pos), default=len(text))
    return start, end


def _subject(before: str, idx: _Index) -> str | None:
    """Who a direction word describes: the driver (code or team) named last before it in its
    sentence as a SUBJECT. A name right after a comparison preposition ("on PIA", "than PIA",
    "to PIA") is the object and is skipped. None when no subject is named: the target, by default."""
    last: tuple[int, str] | None = None
    for pattern, side in idx.subjects:
        for m in pattern.finditer(before):
            if OBJECT_PREPOSITION.search(before[:m.start()]):
                continue
            if last is None or m.start() > last[0]:
                last = (m.start(), side)
    return None if last is None else last[1]


def direction_matches(text: str) -> list[tuple[DirectionEntry, re.Match[str]]]:
    """Lexicon matches in entry order; a matched span is consumed (specific before generic)."""
    consumed: list[tuple[int, int]] = []
    out: list[tuple[DirectionEntry, re.Match[str]]] = []
    for entry in lexicon().entries:
        for m in entry.pattern.finditer(text):
            s, e = m.span()
            if any(s < ce and cs < e for cs, ce in consumed):
                continue
            consumed.append((s, e))
            out.append((entry, m))
    return out


@dataclass
class _Grounded:
    token: NumberToken
    facts: list[Fact]                # cited facts the token's digits and unit match


def _check_bullet(bullet: Bullet, section: str, i: int, idx: _Index) -> tuple[list[Violation], int]:
    out: list[Violation] = []
    text = bullet.text

    def v(rule: str, detail: str) -> None:
        out.append(Violation(rule, section, i, detail))

    # 10 prompt injection
    m = INJECTION.search(text)
    if m:
        v("prompt_injection", f"control phrase or URL: '{m.group(0)}'")

    # 2 fact ids
    cited: list[Fact] = []
    cited_cat: list[CategoricalFact] = []
    for fid in bullet.fact_ids:
        if fid in idx.facts:
            cited.append(idx.facts[fid])
        elif fid in idx.categorical:
            cited_cat.append(idx.categorical[fid])
        else:
            v("unknown_fact_id", f"'{fid}' is not in the FactSheet")
    cited_prefixes = {f.id.split(".")[0] for f in cited} | {c.id.split(".")[0] for c in cited_cat}
    sector_cited = {int(m.group(1)) for f in cited for m in [re.search(r"sector_(\d)_", f.id)] if m}

    # 3b labels
    for pattern, fmt in LABEL_PATTERNS:
        for m in pattern.finditer(text):
            prefix = fmt.format(int(m.group(1)))
            if prefix not in idx.prefixes:
                v("ungrounded_label", f"'{m.group(0)}' is not a segment of this comparison")
            elif prefix not in cited_prefixes and not (prefix.startswith("SEC") and int(prefix[3:]) in sector_cited):
                v("ungrounded_label", f"'{m.group(0)}' is named but none of its facts is cited")

    # 3 / 4 / 7 numbers
    tokens = parse_numbers(text, ignore=idx.masks)
    nulls = [f for f in cited if f.value is None]
    grounded: list[_Grounded] = []
    for tok in tokens:
        if tok.kind == "word":
            if not tok.allowed:
                v("style", f"spelled-out number '{tok.raw}': write digits from the facts")
            continue
        matching = [f for f in cited if _matches(tok, f)]
        ok_unit = [f for f in matching if _unit_ok(tok, f)]
        if ok_unit:
            grounded.append(_Grounded(tok, ok_unit))
            continue
        if tok.unit == "%" and tok.magnitude_text in {INTERVAL_LEVELS[f.id] for f in cited if f.id in INTERVAL_LEVELS}:
            continue                                            # "95% CI", "80% interval" of cited interval facts
        if tok.unit in (None, "lap", "laps") and tok.kind == "number" and float(tok.value).is_integer() \
                and int(tok.value) in idx.context_laps and (tok.unit == "lap" or re.search(r"\blaps?\s*$", text[:tok.start], re.I)):
            continue                                            # "lap 39": the comparison's own laps
        if matching:
            units = ", ".join(sorted({f.unit for f in matching}))
            v("unit_mismatch", f"'{tok.raw}' matches {matching[0].id} but that fact is in {units}")
            continue
        null_for_unit = [f for f in nulls if tok.unit is None or f.unit in UNIT_COMPAT.get(tok.unit, frozenset({tok.unit}))]
        if null_for_unit:
            v("unavailable_data", f"'{tok.raw}' is given for {null_for_unit[0].id}, which is unavailable")
        else:
            v("ungrounded_number", f"'{tok.raw}' is not a display value of any fact cited in this bullet")

    # 7 null facts must be stated as unavailable
    if nulls and not UNAVAILABLE_MARKER.search(text):
        v("unavailable_data", f"cites {nulls[0].id}, which is unavailable, without saying so")

    # 5 direction: explicit signs, then the lexicon
    for g in grounded:
        if g.token.sign:
            signed = [f for f in g.facts if f.value]
            if signed and all((float(f.value) > 0) != (g.token.sign > 0) for f in signed):  # type: ignore[arg-type]
                v("direction_mismatch", f"'{g.token.raw}' has the opposite sign of {signed[0].id}")
    lex = lexicon()
    for entry, m in direction_matches(text):
        s, e = m.span()
        lo, hi = _sentence_bounds(text, s)
        sign, how = entry.sign, list[str]()
        if DIRECTION_NEGATION.search(" ".join(text[lo:s].split()[-2:])):      # "did not lose time"
            sign, how = -sign, how + ["negated"]
        if _subject(text[lo:s], idx) == "reference":                          # "PIA was faster than LEC"
            sign, how = -sign, how + ["said of the reference"]
        family = lex.families[entry.family]
        fam = [f for f in cited if family.search(f.id) and f.value]
        counts = [g for g in grounded if lo <= g.token.start < hi and any(COUNT_FACT.search(f.id) for f in g.facts)]
        timed = [g for g in grounded if lo <= g.token.start < hi and any(f in fam for f in g.facts)]

        def dist(g: _Grounded) -> int:
            return min(abs(g.token.start - s), abs(g.token.start - e))

        if entry.family == "time" and counts and (not timed or min(map(dist, counts)) < min(map(dist, timed))):
            # "lost time in 11 corners": the count must be the corners-lost count, "gained in 3" corners-gained
            best_count = min(counts, key=dist)
            want = "corners_lost" if sign > 0 else "corners_gained"
            if not any(f.id.endswith(want) for f in best_count.facts):
                v("direction_mismatch", f"'{m.group(0)} … {best_count.token.raw}' does not match LAP.{want}")
            continue
        if not fam:
            continue
        if timed:
            best = min(timed, key=dist)
            check = [f for f in best.facts if f in fam]
        else:
            check = fam
        if all((float(f.value) > 0) != (sign > 0) for f in check):  # type: ignore[arg-type]
            qualifier = f" ({', '.join(how)})" if how else ""
            v("direction_mismatch", f"'{m.group(0)}'{qualifier} contradicts {check[0].id} "
                                    f"({'positive' if float(check[0].value) > 0 else 'negative'})")  # type: ignore[arg-type]

    # 6 causal claims (only about performance: the bullet cites a segment)
    segments = {p for p in cited_prefixes if SEGMENT_PREFIX.match(p)}
    if segments:
        allowed = frozenset().union(*(idx.causes.get(p, frozenset()) for p in segments))
        for m in CAUSAL_MARKER.finditer(text):
            before = text[max(0, m.start() - 40): m.start()]
            if NEGATION_BEFORE.search(" ".join(before.split()[-5:])):
                continue
            hedged = bool(HEDGE_BEFORE.search(" ".join(before.split()[-5:]))) or "contributing factor" in m.group(0).lower()
            lo, hi = _sentence_bounds(text, m.start())
            clause = text[lo:hi]
            bad_kw = lex.unsupported_causes.search(clause)
            groups: dict[str, frozenset[str]] = {}
            taken: list[tuple[int, int]] = []
            for name, pat, gcauses in lex.cause_groups:      # specific groups consume their span first
                for gm in pat.finditer(clause):
                    if not any(gm.start() < te and ts < gm.end() for ts, te in taken):
                        taken.append(gm.span())
                        groups[name] = gcauses
            if bad_kw:
                v("unsupported_causal_claim", f"'{bad_kw.group(0)}' is not a cause the findings report")
                break
            if not allowed:
                v("unsupported_causal_claim", f"causal claim '{m.group(0)}' for a segment with no listed cause")
                break
            if not groups:
                if not (hedged and "contributing factor" in clause.lower()):
                    v("unsupported_causal_claim", f"causal claim '{m.group(0)}' names no listed cause")
                    break
                continue
            unlisted = [g for g, gcauses in groups.items() if not (gcauses & allowed)]
            if unlisted:
                v("unsupported_causal_claim", f"'{unlisted[0]}' is not among the listed causes ({', '.join(sorted(allowed))})")
                break

    # 8 scope and safety
    for category, pattern in SCOPE.items():
        m = pattern.search(text)
        if m:
            v("out_of_scope", f"{category}: '{m.group(0)}'")
    if section == "strategy_options" and not STRATEGY_HEDGE.search(text):
        v("out_of_scope", "hedge: strategy options must use hedged language (e.g. 'indicative', 'may')")

    # 9 style (per bullet)
    words = len(text.split())
    if words > MAX_BULLET_WORDS:
        v("style", f"{words} words (max {MAX_BULLET_WORDS})")
    return out, words


def _bullets(report: Report) -> list[tuple[str, int, Bullet]]:
    out = [("headline", 0, report.headline)]
    for section in SECTIONS[1:]:
        out += [(section, i, b) for i, b in enumerate(getattr(report, section))]
    return out


def validate_report(report: Report | dict[str, Any], fs: FactSheet) -> ValidationResult:
    violations: list[Violation] = []
    if not isinstance(report, Report):
        try:
            report = Report.model_validate(report)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                violations.append(Violation("schema", None, None, f"{loc}: {err['msg']}"))
            return ValidationResult(False, violations)
    idx = _index(fs)
    if report.factsheet_hash != idx.factsheet_hash:
        violations.append(Violation("schema", None, None, "factsheet_hash does not match the FactSheet"))
    total = 0
    for section, i, bullet in _bullets(report):
        found, words = _check_bullet(bullet, section, i if section != "headline" else None, idx)  # type: ignore[arg-type]
        violations += found
        total += words
    if total > MAX_REPORT_WORDS:
        violations.append(Violation("style", None, None, f"total: report is {total} words (max {MAX_REPORT_WORDS})"))
    return ValidationResult(not violations, violations)


def explain_for_retry(violations: list[Violation], limit: int = 12) -> str:
    """Short, deduplicated feedback for a retry prompt. Names rules, places and offending text;
    never lists the FactSheet."""
    seen: set[tuple[str, str, str]] = set()
    lines: list[str] = []
    for v in violations:
        key = (v.rule, v.where, v.detail)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {v.where}: {v.rule} — {v.detail}")
    more = len(lines) - limit
    lines = lines[:limit] + ([f"- …and {more} more"] if more > 0 else [])
    head = "The report was rejected by the validator. Fix exactly these problems and keep everything else:"
    rules = {v.rule for v in violations}
    tips = []
    if rules & {"ungrounded_number", "unit_mismatch", "unavailable_data"}:
        tips.append("Use only the display strings of facts you cite in the same bullet, with their units.")
    if "direction_mismatch" in rules:
        tips.append("Describe differences from the target's point of view: positive time loss = slower / lost.")
    if "unsupported_causal_claim" in rules:
        tips.append("Only name causes listed in the cited segment's causes, or drop the causal wording.")
    return "\n".join([head, *lines, *tips])

