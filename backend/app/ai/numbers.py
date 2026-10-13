"""Number formatting and extraction for the AI race engineer (Phase 7A/7B).

display_forms(value, unit)  the exact digit strings a report may use for a fact: the primary
                            precision first, then coarser roundings that are still true
                            (they round-trip to the value within their own precision and never
                            turn a non-zero value into zero). Magnitudes only: a sign is said in
                            words ("lost", "faster") or with a sign that matches the value.
parse_numbers(text)         every numeric token in a text, with sign, unit and kind, so the
                            validator (7B) can check each one against the FactSheet. Labels
                            ("T7", "Turn 7", "Sector 2", "S2", "STR1") are not numbers;
                            "lap 24" IS a number (a lap fact). Spelled-out quantities are
                            tokens too: a few ordinal/small words are allowed
                            (normalize_number_word), every other one is flagged as rejected.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------- display forms
PRIMARY_DECIMALS: dict[str, int] = {
    "s": 3, "s/lap": 3, "km/h": 1, "m": 1, "°C": 1, "m/s²": 1,
    "lap": 0, "laps": 0, "count": 0, "gear": 0, "rank": 0, "corner": 0, "year": 0,
    "ratio": 4, "fraction": 2, "%": 1,
}
# coarser roundings permitted per unit (in addition to the primary precision)
COARSER_DECIMALS: dict[str, tuple[int, ...]] = {
    "s": (2, 1), "s/lap": (2,), "km/h": (0,), "m": (0,), "°C": (0,), "m/s²": (0,), "%": (0,),
    "ratio": (3, 2), "fraction": (1,),
}


def _fmt(magnitude: float, decimals: int) -> str:
    return f"{magnitude:.{decimals}f}"


def display_forms(value: float | int | None, unit: str) -> list[str]:
    """Allowed digit strings for a value, primary precision first. [] for null / non-finite."""
    if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
        return []
    if unit not in PRIMARY_DECIMALS:
        raise ValueError(f"no display precision for unit {unit!r}")
    magnitude = abs(float(value))
    primary = PRIMARY_DECIMALS[unit]
    forms = [_fmt(magnitude, primary)]
    for d in COARSER_DECIMALS.get(unit, ()):
        if d >= primary:
            continue
        text = _fmt(magnitude, d)
        if magnitude > 0 and float(text) == 0.0:
            continue                              # would read as zero
        if abs(float(text) - magnitude) > 0.5 * 10 ** -d + 1e-12:
            continue                              # not a correct rounding (cannot happen, kept as a guard)
        if text not in forms:
            forms.append(text)
    return forms


# ---------------------------------------------------------------- number words
ALLOWED_NUMBER_WORDS: dict[str, int] = {"one": 1, "two": 2, "three": 3, "first": 1, "second": 2}
REJECTED_NUMBER_WORDS = frozenset({
    "zero", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty",
    "seventy", "eighty", "ninety", "hundred", "thousand", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "tenths", "hundredth", "hundredths", "thousandth", "thousandths", "half", "quarter",
    "dozen", "couple", "twice", "thrice", "double", "triple",
})


def normalize_number_word(word: str) -> int | None:
    """The value of an allowed number word ("two" → 2, "first" → 1); None for anything else."""
    return ALLOWED_NUMBER_WORDS.get(word.strip().lower())


# ---------------------------------------------------------------- extraction
Kind = Literal["number", "laptime", "ordinal", "word"]


@dataclass(frozen=True)
class NumberToken:
    raw: str                 # the matched text
    value: float             # signed value (lap times in seconds)
    magnitude_text: str      # digits as written, without sign or thousands separators ("0.213", "1234.5")
    sign: int                # -1, +1, or 0 when no sign was written
    unit: str | None         # normalized unit ("s", "m", "km/h", "%", "lap", "s/lap", "°C", …) or None
    kind: Kind
    allowed: bool            # False only for rejected number words
    start: int
    end: int


_UNIT_ALIASES: list[tuple[str, str]] = [
    (r"s\s*/\s*lap|s\s+per\s+lap|seconds?\s+per\s+lap|sec\s+per\s+lap", "s/lap"),
    (r"km\s*/\s*h|kph|kmh", "km/h"),
    (r"m\s*/\s*s(?:²|\^2|2)", "m/s²"),
    (r"°\s*c|degrees?\s+c(?:elsius)?|deg\s*c", "°C"),
    (r"%|per\s*cent|percent", "%"),
    (r"seconds?|secs?|s(?![a-z])", "s"),
    (r"metres?|meters?|m(?![a-z/])", "m"),
    (r"laps?(?![a-z])", "laps"),
]
_UNIT_RE = re.compile(r"\s*(" + "|".join(f"(?:{p})" for p, _ in _UNIT_ALIASES) + r")", re.IGNORECASE)

# a label word followed by a number names a thing, it is not a quantity
_LABEL_BEFORE = re.compile(r"(?:\b(?:turn|turns|corner|corners|sector|sectors|straight|straights|stint|stints|"
                           r"car|number|no\.|round|gear|g)\s*|#)$", re.IGNORECASE)
_LAP_BEFORE = re.compile(r"\b(?:lap|laps|on\s+lap|from\s+lap|by\s+lap)\s*$", re.IGNORECASE)

_NUMBER_RE = re.compile(
    r"(?P<sign>[+\-−±])?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?::\d{2}(?:\.\d+)?)?(?:\.\d+)?|\.\d+)"
    r"(?P<ord>st|nd|rd|th)?",
)
_WORD_RE = re.compile(r"\b[a-zA-Z]+\b")


def _unit_after(text: str, end: int) -> tuple[str | None, int]:
    m = _UNIT_RE.match(text, end)
    if not m:
        return None, end
    found = m.group(1).lower()
    for pattern, unit in _UNIT_ALIASES:
        if re.fullmatch(pattern, found, re.IGNORECASE):
            return unit, m.end()
    return None, end


def _is_ordinal_second(text: str, start: int) -> bool:
    """'second' is an ordinal ("the second sector") unless it is a time quantity: "a second",
    "one second", "per second", "half a second", "0.5 second"."""
    before = text[:start].rstrip().lower()
    return not (re.search(r"\b(?:a|one|per|half\s+a)$", before) or re.search(r"\d$", before))


def parse_numbers(text: str, ignore: tuple[str, ...] = ()) -> list[NumberToken]:
    """Every numeric token in text, left to right. `ignore` masks literal substrings first
    (e.g. a session key "2024-05-Q" or an event name) so they are not read as numbers."""
    masked = text
    for literal in ignore:
        if literal:
            masked = masked.replace(literal, " " * len(literal))
    tokens: list[NumberToken] = []
    for m in _NUMBER_RE.finditer(masked):
        start, end = m.start(), m.end()
        num, sign_ch, ordinal = m.group("num"), m.group("sign"), m.group("ord")
        prev = masked[start - 1] if start else ""
        if prev.isalpha() or prev == "_" or (sign_ch is None and prev.isdigit()):
            continue                                      # T7, S2, STR1, P3: identifiers
        if sign_ch in ("-", "−") and start and (masked[start - 1].isdigit() or masked[start - 1] in "–-"):
            sign_ch = None                                # "0.05-0.13": a range dash, not a minus
            start += 1
        nxt = masked[end] if end < len(masked) else ""
        if nxt.isalpha() and not ordinal and not _UNIT_RE.match(masked, end):
            continue                                      # "2x", "3D": not a measurement
        if _LABEL_BEFORE.search(masked[:start]):
            continue                                      # Turn 7, Sector 2, Stint 2, gear 3
        sign = {None: 0, "+": 1, "-": -1, "−": -1, "±": 0}[sign_ch]
        unit: str | None
        if ":" in num:                                    # 1:33.660 lap time
            minutes, rest = num.split(":")
            value, unit, stop = int(minutes) * 60 + float(rest), "s", end
            kind: Kind = "laptime"
            mag = num
        else:
            mag = num.replace(",", "")
            value = float(mag)
            kind = "ordinal" if ordinal else "number"
            unit, stop = _unit_after(masked, end) if not ordinal else (None, end)
            if unit is None and not ordinal and _LAP_BEFORE.search(masked[:start]):
                unit = "lap"
        signed = -value if sign < 0 else value
        tokens.append(NumberToken(masked[start:stop] if stop > end else masked[start:end], signed, mag, sign, unit,
                                  kind, True, start, stop))
    for w in _WORD_RE.finditer(masked):
        word = w.group(0).lower()
        if word in ALLOWED_NUMBER_WORDS:
            if word == "second" and not _is_ordinal_second(masked, w.start()):
                tokens.append(NumberToken(w.group(0), 1.0, "1", 0, "s", "word", False, w.start(), w.end()))
                continue
            tokens.append(NumberToken(w.group(0), float(ALLOWED_NUMBER_WORDS[word]), w.group(0), 0, None, "word", True,
                                      w.start(), w.end()))
        elif word in REJECTED_NUMBER_WORDS:
            tokens.append(NumberToken(w.group(0), math.nan, w.group(0), 0, None, "word", False, w.start(), w.end()))
    return sorted(tokens, key=lambda t: t.start)
