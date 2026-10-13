"""Phase 7C: the deterministic template report. No LLM, always valid, good enough to ship alone.

    build_template_report(factsheet, max_words=450) -> Report(generated_by="template")

It is the safety net of 7D (any failure degrades to it) and the UI's development backend.
Rules every sentence follows (docs/fallback_templates.md):
  * every number is display[0] of a fact cited in the same bullet;
  * directions are words ("18.0 m earlier", "lost", "faster"), never a sign in prose;
  * a segment is described only through the metrics behind its triggered causes; the wording
    comes from the metric's sign, so it cannot contradict the data;
  * no causal language (no "because"): metrics are stated alongside the loss;
  * reports describe the target; suggestions name the reference as the subject of what it did;
  * null facts are cited with an explicit availability phrase, never a number.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .schemas import Bullet, CategoricalFact, Fact, FactSheet, Report
from .validator import MAX_BULLET_WORDS, MAX_REPORT_WORDS

TEMPLATE_VERSION = "template-v1"
MAX_SEGMENTS = 3
MAX_LIMITATIONS = 5

# cause → (metric field, wording when the difference is positive, when negative)
CAUSE_METRIC: dict[str, str] = {
    "early_braking_loss": "braking_start_diff_m", "late_braking_gain": "braking_start_diff_m",
    "low_min_speed": "min_speed_diff_kph", "high_min_speed": "min_speed_diff_kph",
    "late_throttle": "time_to_full_throttle_diff_s", "early_throttle": "time_to_full_throttle_diff_s",
    "lower_exit_speed": "exit_speed_diff_kph", "higher_exit_speed": "exit_speed_diff_kph",
    "straight_line_deficit": "top_speed_diff_kph", "straight_line_advantage": "top_speed_diff_kph",
}
FRAGMENTS: dict[str, Callable[[str, bool], str]] = {     # (display, positive?) → what the TARGET did
    "braking_start_diff_m": lambda d, pos: f"braked {d} m {'later' if pos else 'earlier'}",
    "min_speed_diff_kph": lambda d, pos: f"carried {d} km/h {'more' if pos else 'less'} minimum speed",
    "time_to_full_throttle_diff_s": lambda d, pos: f"reached full throttle {d} s {'later' if pos else 'earlier'}",
    "exit_speed_diff_kph": lambda d, pos: f"exited {d} km/h {'faster' if pos else 'slower'}",
    "top_speed_diff_kph": lambda d, pos: f"reached a top speed {d} km/h {'higher' if pos else 'lower'}",
}
GERUNDS: dict[str, Callable[[str, bool], str]] = {       # the same, for a "despite …" clause
    "braking_start_diff_m": lambda d, pos: f"braking {d} m {'later' if pos else 'earlier'}",
    "min_speed_diff_kph": lambda d, pos: f"carrying {d} km/h {'more' if pos else 'less'} minimum speed",
    "time_to_full_throttle_diff_s": lambda d, pos: f"reaching full throttle {d} s {'later' if pos else 'earlier'}",
    "exit_speed_diff_kph": lambda d, pos: f"exiting {d} km/h {'faster' if pos else 'slower'}",
    "top_speed_diff_kph": lambda d, pos: f"with a top speed {d} km/h {'higher' if pos else 'lower'}",
}
# does a POSITIVE difference help the target? (braking later, more minimum/exit/top speed help;
# reaching full throttle later does not)
POSITIVE_HELPS = {"braking_start_diff_m": True, "min_speed_diff_kph": True, "time_to_full_throttle_diff_s": False,
                  "exit_speed_diff_kph": True, "top_speed_diff_kph": True}
# driving suggestions for loss causes: advice variants (used in turn when a cause repeats) and the
# evidence as a function of (target, reference, display)
SUGGESTIONS: dict[str, tuple[tuple[str, ...], Callable[[str, str, str], str]]] = {
    "early_braking_loss": (("Test a later brake point in {label}", "Experiment with a later brake point into {label}",
                            "Review the brake point for {label}"),
                           lambda tgt, ref, d: f"{ref} braked {d} m later"),
    "low_min_speed": (("Work on minimum speed in {label}", "Carry more speed through the middle of {label}",
                       "Focus on the slowest point of {label}"),
                      lambda tgt, ref, d: f"{ref} carried {d} km/h more minimum speed"),
    "late_throttle": (("Aim for full throttle sooner out of {label}", "Look for an earlier full-throttle point out of {label}",
                       "Review throttle application out of {label}"),
                      lambda tgt, ref, d: f"{tgt} took {d} s longer to reach full throttle"),
    "lower_exit_speed": (("Prioritise the exit of {label}", "Work on exit speed out of {label}",
                          "Focus on the run out of {label}"),
                         lambda tgt, ref, d: f"{ref} exited {d} km/h faster"),
    "straight_line_deficit": (("Check straight-line speed on {label}", "Review straight-line speed on {label}",
                               "Look at the run along {label}"),
                              lambda tgt, ref, d: f"{ref} reached a top speed {d} km/h higher"),
}
SUGGESTION_SIGN = {"early_braking_loss": -1, "low_min_speed": -1, "late_throttle": 1, "lower_exit_speed": -1,
                   "straight_line_deficit": -1}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


@dataclass
class _Sheet:
    fs: FactSheet
    facts: dict[str, Fact]
    cats: dict[str, CategoricalFact]

    def fact(self, fid: str) -> Fact | None:
        return self.facts.get(fid)

    def value(self, fid: str) -> float | int | None:
        f = self.facts.get(fid)
        return None if f is None else f.value

    def shown(self, fid: str) -> str:
        f = self.facts[fid]
        return f.display[0]

    def cat(self, fid: str) -> object:
        c = self.cats.get(fid)
        return None if c is None else c.value

    def label(self, prefix: str) -> str:
        return str(self.cat(f"{prefix}.label") or prefix)

    def causes(self, prefix: str) -> list[str]:
        value = self.cat(f"{prefix}.causes")
        return [c for c in value if c != "unclear"] if isinstance(value, list) else []


def _segment_bullet(s: _Sheet, prefix: str, target: str) -> Bullet | None:
    """`{label}: {T} lost|gained {d} s ({what explains it})[, despite {what went the other way}].`"""
    loss_id = f"{prefix}.time_loss_s"
    loss = s.value(loss_id)
    if loss is None or loss == 0:
        return None
    gained = loss < 0
    ids = [loss_id]
    explain: list[str] = []
    despite: list[str] = []
    seen: set[str] = set()
    for cause in s.causes(prefix):
        field = CAUSE_METRIC.get(cause)
        if field is None or field in seen:
            continue
        seen.add(field)
        fid = f"{prefix}.{field}"
        v = s.value(fid)
        if v is None or v == 0:
            continue
        helps = (v > 0) == POSITIVE_HELPS[field]
        if helps == gained:                     # in line with the result: it is part of the explanation
            explain.append(FRAGMENTS[field](s.shown(fid), v > 0))
        else:                                   # went the other way: said as "despite …"
            despite.append(GERUNDS[field](s.shown(fid), v > 0))
        ids.append(fid)
    text = f"{s.label(prefix)}: {target} {'gained' if gained else 'lost'} {s.shown(loss_id)} s"
    if explain:
        text += f" ({'; '.join(explain)})"
    if despite:
        text += f", despite {_join(despite)}"
    if not explain and not despite and isinstance(raw := s.cat(f"{prefix}.causes"), list) and "unclear" in raw:
        text += f"; the time {'gain' if gained else 'loss'} has no single clear driving cause in the available data"
    return Bullet(text=text + ".", fact_ids=ids)


def _headline(s: _Sheet, target: str, reference: str) -> Bullet:
    """The lap delta, then the biggest sector that went the SAME way as the lap; only when no sector
    did is the biggest opposite sector given, as a contrast ("despite gaining the most in …")."""
    fid = "LAP.time_loss_s"
    v = s.value(fid)
    if v is None:
        return Bullet(text=f"The lap time difference between {target} and {reference} is unavailable.", fact_ids=[fid])
    shown = s.shown(fid)
    if float(shown) == 0.0:
        return Bullet(text=f"{target} and {reference} set the same lap time ({shown} s apart).", fact_ids=[fid])
    slower = v > 0
    text = f"{target} was {shown} s {'slower' if slower else 'faster'} than {reference} over the lap"
    ids = [fid]
    sectors = [(abs(float(x)), i, x > 0) for i in (1, 2, 3)
               if (x := s.value(f"LAP.sector_{i}_delta_s")) is not None and x != 0]
    same = [t for t in sectors if t[2] == slower]
    if same:
        _, i, _ = max(same)
        text += f", {'losing' if slower else 'gaining'} the most in Sector {i} ({s.shown(f'LAP.sector_{i}_delta_s')} s)"
        ids.append(f"LAP.sector_{i}_delta_s")
    elif sectors:
        _, i, _ = max(sectors)
        text += (f", despite {'gaining' if slower else 'losing'} the most time in Sector {i} "
                 f"({s.shown(f'LAP.sector_{i}_delta_s')} s)")
        ids.append(f"LAP.sector_{i}_delta_s")
    return Bullet(text=text + ".", fact_ids=ids)


def _suggestions(s: _Sheet, target: str, reference: str) -> list[Bullet]:
    """One per top-loss segment. Each picks a cause not used yet when it can (so three corners that
    all lack minimum speed do not give three identical tips); a repeated cause gets the next
    wording variant."""
    candidates: list[tuple[str, list[str]]] = []
    for prefix in s.fs.rankings.largest_losses:
        usable = [c for c in s.causes(prefix) if c in SUGGESTIONS
                  and (v := s.value(f"{prefix}.{CAUSE_METRIC[c]}")) is not None and v != 0
                  and (v > 0) == (SUGGESTION_SIGN[c] > 0)]
        if usable:
            candidates.append((prefix, usable))
    out: list[Bullet] = []
    used: dict[str, int] = {}
    for prefix, usable in candidates[:MAX_SEGMENTS]:
        cause = next((c for c in usable if c not in used), usable[0])
        variants, evidence = SUGGESTIONS[cause]
        advice = variants[used.get(cause, 0) % len(variants)]
        used[cause] = used.get(cause, 0) + 1
        fid = f"{prefix}.{CAUSE_METRIC[cause]}"
        out.append(Bullet(text=f"{advice.format(label=s.label(prefix))}; {evidence(target, reference, s.shown(fid))}.",
                          fact_ids=[fid]))
    return out


def _tire(s: _Sheet) -> tuple[list[Bullet], list[Bullet]]:
    tire = s.fs.tire
    status = tire.get("status")
    if status == "unavailable" or status is None:
        reason = str(tire.get("reason") or "no tyre forecast for this comparison")
        return [Bullet(text=f"Tyre forecast unavailable: {reason}.", fact_ids=["tire.deg_s_per_lap"])], []
    if status == "insufficient_data":
        used, req = s.value("tire.clean_laps_used"), s.value("tire.clean_laps_required")
        if used is not None and req is not None:
            text = (f"No tyre forecast yet: {used} of the {_plural(int(req), 'required clean lap')} "
                    f"in the current stint.")
            return [Bullet(text=text, fact_ids=["tire.clean_laps_used", "tire.clean_laps_required", "tire.deg_s_per_lap"])], []
        return [Bullet(text=f"No tyre forecast yet: {s.cat('tire.reason') or 'not enough data'}.",
                       fact_ids=["tire.deg_s_per_lap"])], []
    notes: list[Bullet] = []
    deg = s.value("tire.deg_s_per_lap")
    compound = str(s.cat("tire.compound") or "current").lower()
    if deg is None:
        notes.append(Bullet(text="Tyre degradation is unavailable for this forecast.", fact_ids=["tire.deg_s_per_lap"]))
    else:
        shown = s.shown("tire.deg_s_per_lap")
        trend = (f"lap times grow by {shown} s/lap" if deg > 0 else
                 f"lap times improve by {shown} s/lap" if deg < 0 else f"lap times are flat ({shown} s/lap)")
        ids = ["tire.deg_s_per_lap"]
        text = f"On the {compound} tyre, {trend}"
        lo, hi = s.value("tire.deg_ci_low"), s.value("tire.deg_ci_high")
        if lo is not None and hi is not None:
            def bound(fid: str, v: float | int) -> str:
                return f"{s.shown(fid)} s/lap" + (" of improvement" if v < 0 else "")
            text += f" (95% CI {bound('tire.deg_ci_low', lo)} to {bound('tire.deg_ci_high', hi)})"
            ids += ["tire.deg_ci_low", "tire.deg_ci_high"]
        notes.append(Bullet(text=text + ": a combined adjusted trend, not pure tyre wear.", fact_ids=ids))
    if s.cat("tire.rainfall_in_stint") is True:
        notes.append(Bullet(text="Rain was reported in this stint; the tyre model is trained on dry races only.",
                            fact_ids=["tire.rainfall_in_stint"]))
    strategy: list[Bullet] = []
    e = s.value("tire.window_earliest_lap")
    ids = ["tire.window_earliest_lap", "tire.window_central_lap", "tire.window_latest_lap", "tire.confidence"]
    if e is None:
        reason = str(s.cat("tire.window_reason") or "unavailable").replace("_", " ")
        notes.append(Bullet(text=f"No pit window: {reason}.", fact_ids=["tire.window_earliest_lap"]))
        return notes, strategy
    c, latest = s.value("tire.window_central_lap"), s.value("tire.window_latest_lap")
    conf = s.cat("tire.confidence")
    central_txt = f"central lap {c}" if c is not None else "central lap beyond the forecast horizon"
    latest_txt = f"latest lap {latest}" if latest is not None else "latest lap open-ended"
    notes.append(Bullet(text=f"Heuristic pit window: earliest lap {e}, {central_txt}, {latest_txt} (confidence: {conf}).",
                        fact_ids=ids))
    notes.append(Bullet(text="The window is a heuristic that ignores undercut and overcut, traffic, track position, "
                             "safety-car probability and tyre availability.", fact_ids=["tire.window_assumptions"]))
    if latest is not None:
        span = f"laps {e}–{latest}, {central_txt}"
    elif c is not None:
        span = f"from lap {e}, {central_txt}, open-ended beyond the forecast horizon"
    else:
        span = f"from lap {e}; the central lap is beyond the forecast horizon"
    strategy.append(Bullet(text=f"Indicative pit window: {span} (confidence: {conf}); it ignores traffic and safety-car risk.",
                           fact_ids=ids))
    return notes, strategy


def _limitations(s: _Sheet, reported: list[str]) -> list[Bullet]:
    fs = s.fs
    out = [Bullet(text="Traffic data is unavailable, so no time loss is attributed to traffic.", fact_ids=["session.notes"])]
    out.append(Bullet(text="Listed causes are rule-based contributing factors, not proof.", fact_ids=["data_quality.notes"]))
    if s.cat("quality.sector_check_ok") is False and s.value("quality.sector_check_max_discrepancy_s") is not None:
        fid = "quality.sector_check_max_discrepancy_s"
        out.append(Bullet(text=f"Corner-level times carry up to {s.shown(fid)} s of alignment uncertainty "
                               f"(the sector cross-check is outside tolerance).", fact_ids=[fid]))
    low = fs.data_quality.low_confidence_segments[:4]
    if low:
        more = len(fs.data_quality.low_confidence_segments) - len(low)
        # the count of the rest is not a fact: allowed number words only (never a digit)
        names = [s.label(p) for p in low]
        if more:
            names.append({1: "one other segment", 2: "two other segments",
                          3: "three other segments"}.get(more, "several other segments"))
        out.append(Bullet(text=f"Low confidence: {_join(names)}.", fact_ids=[f"{p}.confidence" for p in low]))
    target, reference = fs.drivers.target.code, fs.drivers.reference.code
    for prefix in reported:
        f = s.fact(f"{prefix}.braking_start_diff_m")
        if f is None or f.value is not None:
            continue
        t_none = s.value(f"{prefix}.target_braking_start_m") is None
        r_none = s.value(f"{prefix}.reference_braking_start_m") is None
        who = "either driver" if t_none == r_none else (target if t_none else reference)
        out.append(Bullet(text=f"{s.label(prefix)}: braking points cannot be compared; no braking was detected for {who}.",
                          fact_ids=[f.id]))
    return out[:MAX_LIMITATIONS]


def _words(b: Bullet) -> int:
    return len(b.text.split())


def build_template_report(fs: FactSheet, max_words: int = MAX_REPORT_WORDS) -> Report:
    s = _Sheet(fs, {f.id: f for f in fs.facts}, {c.id: c for c in fs.categorical_facts})
    target, reference = fs.drivers.target.code, fs.drivers.reference.code
    losses = [b for p in fs.rankings.largest_losses[:MAX_SEGMENTS] if (b := _segment_bullet(s, p, target))]
    gains = [b for p in fs.rankings.largest_gains[:MAX_SEGMENTS] if (b := _segment_bullet(s, p, target))]
    if not losses:
        losses = [Bullet(text=f"No corner cost {target} time beyond the noise floor.", fact_ids=[])]
    if not gains:
        gains = [Bullet(text=f"{target} did not gain time in any corner beyond the noise floor.", fact_ids=[])]
    tire_notes, strategy = _tire(s)
    reported = fs.rankings.largest_losses[:MAX_SEGMENTS] + fs.rankings.largest_gains[:MAX_SEGMENTS]
    sections: dict[str, list[Bullet]] = {
        "biggest_time_loss": losses, "strongest_areas": gains, "tire_notes": tire_notes,
        "driving_suggestions": _suggestions(s, target, reference), "strategy_options": strategy,
        "data_limitations": _limitations(s, reported),
    }
    for bullets in sections.values():
        assert all(_words(b) <= MAX_BULLET_WORDS for b in bullets)
    headline = _headline(s, target, reference)
    # trim to the word budget: lowest-priority bullets go first; every section keeps its first bullet
    # until nothing else is left, then whole sections go in the same order
    order = ["data_limitations", "strongest_areas", "driving_suggestions", "tire_notes", "biggest_time_loss",
             "strategy_options"]

    def total() -> int:
        return _words(headline) + sum(_words(b) for bs in sections.values() for b in bs)

    for keep in (1, 0):
        for name in order:
            while total() > max_words and len(sections[name]) > keep:
                sections[name].pop()
    return Report(headline=headline, generated_by="template", prompt_version=TEMPLATE_VERSION,
                  factsheet_hash=fs.factsheet_hash, **sections)
