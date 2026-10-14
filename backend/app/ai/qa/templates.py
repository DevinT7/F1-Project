"""Phase 7E per-intent template answers: deterministic, validated, ≤ 120 words.

    template_answer(intent, factsheet, slice) -> Bullet
    refusal(category) / clarification(factsheet, unknown) -> Bullet
    follow_ups(intent, factsheet) -> list[FollowUp]

They reuse the 7C sentence builders (fallback.py), so their wording rules are the same: numbers
are display[0] of a cited fact, directions are words chosen from the sign, no causal language
outside the "evidence suggests" phrasing of cause answers.
"""
from __future__ import annotations

import re

from ..fallback import CAUSE_METRIC, FRAGMENTS, _headline, _join, _segment_bullet, _Sheet, _suggestions, _tire
from ..schemas import Bullet, FactSheet
from .evidence import TRAFFIC_FACT, Evidence
from .intents import Intent
from .schemas import MAX_ANSWER_WORDS, FollowUp
from .slices import DIFF_FIELDS, Slice

CAPABILITIES = ("where time was lost or gained, why a sector was faster or slower, what happened in a corner, "
                "which corners to focus on, the current tyre stint, the pit window, and whether the evidence points "
                "to the driver, the tyres or traffic")
REFUSALS = {
    "instructions": "I answer only from the verified data and never guess or invent numbers.",
    "other_session": "Other sessions and races are not in this comparison's data.",
    "other_driver": "That driver is not part of this comparison.",
    "future": "I do not predict future races or championships.",
    "personal": "I do not discuss personal information about anyone.",
    "opinion": "I do not give opinions about people.",
}


GAIN_WORDS = re.compile(r"\b(?:gain\w*|faster|quicker|strongest|best)\b", re.IGNORECASE)
LOSS_WORDS = re.compile(r"\b(?:los[et]\w*|loss\w*|slower|weakest|worst)\b", re.IGNORECASE)


def _sheet(fs: FactSheet) -> _Sheet:
    return _Sheet(fs, {f.id: f for f in fs.facts}, {c.id: c for c in fs.categorical_facts})


def _merge(parts: list[Bullet], limit: int = MAX_ANSWER_WORDS) -> Bullet:
    """Join bullets into one answer, dropping trailing parts that would exceed the word limit."""
    texts: list[str] = []
    ids: list[str] = []
    for b in parts:
        if texts and len(" ".join([*texts, b.text]).split()) > limit:
            break
        texts.append(b.text)
        ids += b.fact_ids
    return Bullet(text=" ".join(texts), fact_ids=list(dict.fromkeys(ids)))


def refusal(category: str | None) -> Bullet:
    lead = REFUSALS.get(category or "")
    text = f"I can only discuss this comparison: {CAPABILITIES}."
    return Bullet(text=f"{lead} {text}" if lead else text, fact_ids=[])


def clarification(fs: FactSheet, unknown: list[str]) -> Bullet:
    s = _sheet(fs)
    kind = unknown[0].split()[0] if unknown else "Turn"
    prefix = {"Turn": "T", "Sector": "SEC", "Straight": "STR"}.get(kind, "T")
    have = sorted(int(p[len(prefix):]) for p in {f.id.split(".")[0] for f in fs.facts}
                  if p.startswith(prefix) and p[len(prefix):].isdigit())
    noun = {"Turn": "corner", "Sector": "sector", "Straight": "straight"}.get(kind, "segment")
    if not have:
        return Bullet(text=f"That {noun} is not part of this comparison.", fact_ids=[])
    first, last = f"{prefix}{have[0]}", f"{prefix}{have[-1]}"
    return Bullet(text=f"That {noun} is not part of this comparison; it covers {s.label(first)} to {s.label(last)}. "
                       f"Which {noun} did you mean?",
                  fact_ids=[f"{first}.time_loss_s", f"{last}.time_loss_s"])


def _where(s: _Sheet, fs: FactSheet, question: str = "") -> Bullet:
    t, r = fs.drivers.target.code, fs.drivers.reference.code
    parts = [_headline(s, t, r)]
    gains_first = bool(GAIN_WORDS.search(question)) and not LOSS_WORDS.search(question)
    losses = [b for p in fs.rankings.largest_losses[:1 if gains_first else 2] if (b := _segment_bullet(s, p, t))]
    gains = [b for p in fs.rankings.largest_gains[:2 if gains_first else 1] if (b := _segment_bullet(s, p, t))]
    if gains_first and gains:
        parts.append(Bullet(text="Biggest gains:", fact_ids=[]))
        parts += gains
        if losses:
            parts.append(Bullet(text="Biggest loss:", fact_ids=[]))
            parts += losses
        return _merge(parts)
    if losses:
        parts.append(Bullet(text="Biggest losses:", fact_ids=[]))
        parts += losses
    else:
        parts.append(Bullet(text=f"No corner cost {t} time beyond the noise floor.", fact_ids=[]))
    if gains:
        parts.append(Bullet(text="Biggest gain:", fact_ids=[]))
        parts += gains
    return _merge(parts)


def _sector(s: _Sheet, fs: FactSheet, slc: Slice) -> Bullet:
    t, r = fs.drivers.target.code, fs.drivers.reference.code
    sector = next((i.split(".")[0] for i in slc.categorical_ids if i.startswith("SEC") and i.endswith(".label")), None)
    if sector is None:
        return Bullet(text="That sector is not part of this comparison.", fact_ids=[])
    n = sector.removeprefix("SEC")
    fid = f"LAP.sector_{n}_delta_s" if s.value(f"LAP.sector_{n}_delta_s") is not None else f"{sector}.time_loss_s"
    v = s.value(fid)
    label = s.label(sector)
    if v is None:
        return Bullet(text=f"The {label} time difference is unavailable for this comparison.", fact_ids=[fid])
    if v == 0:
        head = Bullet(text=f"In {label}, {t} and {r} were level ({s.shown(fid)} s apart).", fact_ids=[fid])
        same = slc.segments
    else:
        head = Bullet(text=f"In {label}, {t} was {s.shown(fid)} s {'slower' if v > 0 else 'faster'} than {r}.",
                      fact_ids=[fid])
        same = [p for p in slc.segments if (x := s.value(f"{p}.time_loss_s")) is not None and (x > 0) == (v > 0)]
    parts = [head]
    inside = [b for p in same[:3] if (b := _segment_bullet(s, p, t))]
    if inside:
        parts.append(Bullet(text=f"Inside {label}:", fact_ids=[]))
        parts += inside
        parts.append(Bullet(text="The listed metrics are rule-based findings, not proof.", fact_ids=[]))
    else:
        parts.append(Bullet(text=f"No single corner in {label} stands out beyond the noise floor.", fact_ids=[]))
    return _merge(parts)


def _tire_status(s: _Sheet, fs: FactSheet) -> Bullet:
    notes, _ = _tire(s)
    parts = notes[:1]
    left = s.value("tire.remaining_competitive_laps")
    if fs.tire.get("status") == "ok" and left is not None:
        conf = s.cat("tire.confidence")
        word = "lap" if float(left) == 1 else "laps"
        parts.append(Bullet(text=f"The forecast suggests about {s.shown('tire.remaining_competitive_laps')} more "
                                 f"competitive {word} before the central pit lap (confidence: {conf}).",
                            fact_ids=["tire.remaining_competitive_laps", "tire.confidence"]))
    elif fs.tire.get("status") == "ok":
        parts.append(Bullet(text="The forecast does not reach the pit threshold within its horizon, so the "
                                 "remaining competitive laps are open-ended.", fact_ids=["tire.remaining_competitive_laps"]))
    parts += [b for b in notes[1:] if "rain" in b.text.lower()]
    return _merge(parts)


def _focus(s: _Sheet, fs: FactSheet) -> Bullet:
    t, r = fs.drivers.target.code, fs.drivers.reference.code
    tips = _suggestions(s, t, r)
    if not tips:
        losses = [b for p in fs.rankings.largest_losses[:2] if (b := _segment_bullet(s, p, t))]
        if not losses:
            return Bullet(text=f"No corner cost {t} time beyond the noise floor, so there is no clear focus corner.",
                          fact_ids=["LAP.time_loss_s"])
        return _merge([Bullet(text="No listed cause points to a specific technique; the biggest losses were:",
                              fact_ids=[]), *losses])
    low = [p for p in fs.rankings.largest_losses[:3] if s.cat(f"{p}.confidence") == "low"]
    parts = [Bullet(text="Focus areas, biggest loss first:", fact_ids=[]), *tips]
    if low:
        parts.append(Bullet(text=f"Low confidence: {_join([s.label(p) for p in low])}.",
                            fact_ids=[f"{p}.confidence" for p in low]))
    return _merge(parts)


def _pit(s: _Sheet, fs: FactSheet) -> Bullet:
    notes, strategy = _tire(s)
    if strategy:
        assumptions = Bullet(text="It is a heuristic that ignores undercut and overcut, traffic, track position, "
                                  "safety-car probability and tyre availability.", fact_ids=["tire.window_assumptions"])
        return _merge([strategy[0], assumptions])
    return _merge(notes[-1:] if fs.tire.get("status") == "ok" else notes[:1])


def _cause(s: _Sheet, fs: FactSheet, ev: Evidence) -> Bullet:
    parts: list[Bullet] = []
    if ev.driver_factors or ev.tire_factors or ev.conditions:
        lead = ("Evidence suggests one factor" if ev.single_cause_allowed
                else "Evidence suggests these may be contributing factors")
        driver = ev.driver_factors[:3]
        if driver:
            by_segment: dict[str, list[str]] = {}
            for f in driver:                       # "Turn 19: LEC carried … and exited …"
                by_segment.setdefault(f.label, []).append(f.description.split(": ", 1)[1])
            code = f"{fs.drivers.target.code} "
            phrases = [f"{label}: {code}{_join([x.removeprefix(code) for x in d])}" for label, d in by_segment.items()]
            parts.append(Bullet(text=f"{lead}: " + "; ".join(phrases) + ".",
                                fact_ids=[i for f in driver for i in f.fact_ids]))
        if ev.tire_factors:
            head = "Tyres" if driver else f"{lead}, from the tyres"
            parts.append(Bullet(text=f"{head}: " + "; ".join(f.description for f in ev.tire_factors) + ".",
                                fact_ids=[i for f in ev.tire_factors for i in f.fact_ids]))
        if ev.conditions:
            head = "Conditions" if driver or ev.tire_factors else f"{lead}, from the conditions"
            parts.append(Bullet(text=f"{head}: " + "; ".join(f.description for f in ev.conditions) + ".",
                                fact_ids=[i for f in ev.conditions for i in f.fact_ids]))
    else:
        scope = [p for p in ev.scope if s.value(f"{p}.time_loss_s") is not None]
        parts.append(Bullet(text="The data lists no driving, tyre or condition factor for this time difference.",
                            fact_ids=[f"{p}.causes" for p in scope] or ["data_quality.notes"]))
    if not ev.tire_factors:
        t, r = fs.drivers.target.code, fs.drivers.reference.code
        tc, rc = s.cat("target.compound"), s.cat("reference.compound")
        if tc and tc == rc:
            parts.append(Bullet(text=f"{t} and {r} were on the same {str(tc).lower()} compound.",
                                fact_ids=["target.compound", "reference.compound"]))
    if ev.traffic == "unavailable":
        parts.append(Bullet(text="Traffic cannot be assessed: traffic data is unavailable.", fact_ids=[TRAFFIC_FACT]))
    parts.append(Bullet(text=f"Data quality: {ev.data_quality}.", fact_ids=[f"{p}.confidence" for p in ev.scope]))
    # the traffic sentence must survive the word limit: drop from the factor list first
    merged = _merge(parts)
    if ev.traffic == "unavailable" and TRAFFIC_FACT not in merged.fact_ids:
        merged = _merge([parts[0], parts[-2]])
    return merged


def _corner(s: _Sheet, fs: FactSheet, slc: Slice) -> Bullet:
    t = fs.drivers.target.code
    if not slc.segments:
        return Bullet(text="Which corner do you mean?", fact_ids=[])
    prefix = slc.segments[0]
    label = s.label(prefix)
    loss_id = f"{prefix}.time_loss_s"
    if s.value(loss_id) is None:
        return Bullet(text=f"{label}: the time difference is unavailable.", fact_ids=[loss_id])
    head = _segment_bullet(s, prefix, t) or Bullet(text=f"{label}: {t} and the reference were level "
                                                        f"({s.shown(loss_id)} s).", fact_ids=[loss_id])
    used = {i for i in head.fact_ids}
    extra: list[str] = []
    ids: list[str] = []
    for field in DIFF_FIELDS:
        fid = f"{prefix}.{field}"
        v = s.value(fid)
        if field not in FRAGMENTS or fid in used or v is None or v == 0:
            continue
        extra.append(FRAGMENTS[field](s.shown(fid), v > 0))
        ids.append(fid)
    parts = [head]
    if extra:
        parts.append(Bullet(text=f"Also, {t} {_join(extra[:3])}.", fact_ids=ids[:3]))
    nulls = [f"{prefix}.{f}" for f in ("braking_start_diff_m",) if s.fact(f"{prefix}.{f}") is not None
             and s.value(f"{prefix}.{f}") is None]
    if nulls:
        parts.append(Bullet(text="Braking points cannot be compared here: no braking was detected.", fact_ids=nulls))
    parts.append(Bullet(text=f"Confidence: {s.cat(f'{prefix}.confidence')}.", fact_ids=[f"{prefix}.confidence"]))
    return _merge(parts)


def template_answer(intent: Intent, fs: FactSheet, slc: Slice, question: str = "") -> Bullet:
    s = _sheet(fs)
    if intent is Intent.WHERE_LOST_TIME:
        return _where(s, fs, question)
    if intent is Intent.WHY_FASTER_IN_SECTOR:
        return _sector(s, fs, slc)
    if intent is Intent.TIRE_STATUS:
        return _tire_status(s, fs)
    if intent is Intent.FOCUS_CORNERS:
        return _focus(s, fs)
    if intent is Intent.WHEN_TO_PIT:
        return _pit(s, fs)
    if intent is Intent.CAUSE_ATTRIBUTION and slc.evidence is not None:
        return _cause(s, fs, slc.evidence)
    if intent is Intent.CORNER_DETAIL:
        return _corner(s, fs, slc)
    return refusal(None)


# ---------------------------------------------------------------- follow-up suggestions
NEXT: dict[Intent, tuple[Intent, ...]] = {
    Intent.WHERE_LOST_TIME: (Intent.CAUSE_ATTRIBUTION, Intent.FOCUS_CORNERS, Intent.CORNER_DETAIL),
    Intent.WHY_FASTER_IN_SECTOR: (Intent.CORNER_DETAIL, Intent.FOCUS_CORNERS, Intent.CAUSE_ATTRIBUTION),
    Intent.TIRE_STATUS: (Intent.WHEN_TO_PIT, Intent.CAUSE_ATTRIBUTION, Intent.WHERE_LOST_TIME),
    Intent.FOCUS_CORNERS: (Intent.CORNER_DETAIL, Intent.WHY_FASTER_IN_SECTOR, Intent.CAUSE_ATTRIBUTION),
    Intent.WHEN_TO_PIT: (Intent.TIRE_STATUS, Intent.WHERE_LOST_TIME, Intent.FOCUS_CORNERS),
    Intent.CAUSE_ATTRIBUTION: (Intent.CORNER_DETAIL, Intent.FOCUS_CORNERS, Intent.TIRE_STATUS),
    Intent.CORNER_DETAIL: (Intent.CAUSE_ATTRIBUTION, Intent.FOCUS_CORNERS, Intent.WHERE_LOST_TIME),
    Intent.OUT_OF_SCOPE: (Intent.WHERE_LOST_TIME, Intent.FOCUS_CORNERS, Intent.CAUSE_ATTRIBUTION),
}


def follow_up_question(intent: Intent, fs: FactSheet) -> str | None:
    s = _sheet(fs)
    t = fs.drivers.target.code
    race = fs.session.session_type == "R"
    if intent is Intent.WHERE_LOST_TIME:
        return f"Where did {t} lose the most time?"
    if intent is Intent.WHY_FASTER_IN_SECTOR:
        sectors = [(abs(float(v)), i, v > 0) for i in (1, 2, 3) if (v := s.value(f"LAP.sector_{i}_delta_s"))]
        if not sectors:
            return None
        _, i, slower = max(sectors)
        return f"Why was {t} {'slower' if slower else 'faster'} in Sector {i}?"
    if intent is Intent.TIRE_STATUS:
        return f"Is {t}'s current tyre stint still competitive?" if race else None
    if intent is Intent.WHEN_TO_PIT:
        return f"When should {t} pit?" if race and fs.tire.get("status") == "ok" else None
    if intent is Intent.FOCUS_CORNERS:
        return f"Which corners should {t} focus on?"
    if intent is Intent.CAUSE_ATTRIBUTION:
        return "Was the time loss caused by the driver, the tyres or traffic?"
    if intent is Intent.CORNER_DETAIL:
        top = fs.rankings.largest_losses[:1]
        return f"What happened in {s.label(top[0])}?" if top else None
    return None


def follow_ups(intents: list[Intent], fs: FactSheet, current: Intent, limit: int = 3) -> list[FollowUp]:
    out: list[FollowUp] = []
    for intent in [*intents, *NEXT[current]]:
        if intent is current and intent is not Intent.CORNER_DETAIL or any(f.intent == intent.value for f in out):
            continue
        q = follow_up_question(intent, fs)
        if q:
            out.append(FollowUp(intent=intent.value, question=q))  # type: ignore[arg-type]
        if len(out) >= limit:
            break
    return out


__all__ = ["CAUSE_METRIC", "clarification", "follow_ups", "refusal", "template_answer"]
