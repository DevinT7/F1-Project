# Validator rules (Phase 7B)

The validator is the deterministic guardrail that decides whether a generated report (and, in
7E, an answer) may be shown. It was built before any LLM exists. A report either passes every
rule, or it is retried (7D, with `explain_for_retry`) and finally replaced by the template
report (7C). Unchecked LLM text is never shown.

Code: `backend/app/ai/validator.py`, `backend/app/ai/direction_lexicon.yaml`. Corpus:
`backend/tests/ai/corpus/{good,bad}.jsonl`. Tests: `tests/test_ai_validator.py`. Mutation test:
`python -m scripts.validator_mutation_test`.

```python
from app.ai.validator import validate_report, explain_for_retry
result = validate_report(report_dict_or_Report, factsheet)    # ValidationResult(ok, violations)
feedback = explain_for_retry(result.violations)               # for the 7D retry prompt
```

Each `Violation` has `rule`, `section`, `bullet_index` and `detail`. The headline and
report-level problems have `bullet_index = None`.

## Rules

| # | Rule | What it rejects |
|---|---|---|
| 1 | `schema` | anything that is not a `schemas.Report`: extra or missing keys, bullets without `fact_ids`, non-string ids. Also a `factsheet_hash` that is not this FactSheet's. |
| 2 | `unknown_fact_id` | a cited id that is not in the FactSheet |
| 3 | `ungrounded_number` | a number whose digits are not a display string of a fact **cited in the same bullet** |
| 3b | `ungrounded_label` | a corner, sector or straight that does not exist, or whose facts the bullet does not cite (the "wrong corner label" pitfall) |
| 4 | `unit_mismatch` | a number that matches a cited fact only under another unit (0.311 m for a time) |
| 5 | `direction_mismatch` | a direction word or an explicit sign that contradicts the sign of the cited fact |
| 6 | `unsupported_causal_claim` | causal language that names no cause, a cause not listed for the cited segment, or a non-findings cause (tyres, traffic, …) |
| 7 | `unavailable_data` | a number given for a null fact, or a cited null fact without an availability marker |
| 8 | `out_of_scope` | injuries, team politics, rules and penalties, driver intent or internal states, strategy guarantees; strategy options without hedged language |
| 9 | `style` | a bullet over 60 words, a report over 450 words, spelled-out numbers outside the allow-list |
| 10 | `prompt_injection` | control phrases ("ignore previous instructions", "system prompt", …) and any URL or domain |

### Numbers (rules 3, 4, 7)

Numbers come from `numbers.parse_numbers`. Labels such as `T7`, `Turn 7`, `Sector 2` and
`STR1` are not numbers; the label rule checks them separately. Literal identifiers are masked
first, as whole occurrences only:
- segment labels with digits (`Straight 1 (431-2231 m)`);
- the session key, event and date;
- the model version.

A token is **grounded** when its digits equal one of the `display` strings of a fact cited in
the same bullet, and its unit (if written) is that fact's unit. Lap times (`1:46.445`) are
compared in seconds. A percentage is compared with `value × 100` of a 0–1 fraction fact. Signs
are handled by rule 5.

Allow-list (no citation needed):
- **The comparison's own laps:** `lap 39` when 39 is the target or reference lap number, or the
  tyre as-of lap.
- **Interval levels:** "95%" when a degradation-CI fact is cited, "80%" when a next-lap interval
  fact is cited.
- **Number words:** `one`, `two`, `three`, `first`, `second` (ordinal). Every other spelled-out
  number or fraction is a `style` violation, including "a second", "half a second", "a fifth" and
  "zero point two".

An ungrounded token becomes `unavailable_data` instead of `ungrounded_number` when the bullet
cites a null fact of a compatible unit. "The pit window closes at lap 44" for the open-ended
latest lap is a fabricated value for missing data, not merely an uncited number.

Availability markers: unavailable, not available, not detected, no braking, did not brake,
not recorded, no data, missing, open-ended, beyond the horizon, no crossing, not applicable, no
forecast, cannot be compared, not reached, not measured, unknown, no window.

### Direction (rule 5)

Reports describe the **target**: "LEC lost 0.311 s" means a positive time loss. The lexicon
(`direction_lexicon.yaml`) maps descriptive phrases to a fact family and an expected sign.
- **Families:** time, braking, throttle, minimum, entry, exit and top speed, and any speed
  difference.
- **Specific before generic:** entries are matched in file order and a matched span is consumed.
  So "8.0 km/h slower" is a speed statement, never also the time word "slower".
- **Prescriptive phrases:** advice in driving suggestions carries the opposite sign of what
  happened. "Carrying more minimum speed" is right advice when the target had *less*.
- **Every entry has examples.** A test runs the real matcher on them and requires each to yield
  exactly its own direction.

A phrase is checked against the nearest grounded number of its family in the same sentence;
without one, against all cited facts of that family. It is a mismatch only when **every** fact
checked has the opposite sign, so a sentence mentioning a loss and a gain is not misread. Zero
values and families with no cited fact are not checked. An explicit sign ("−0.311 s") must match
the sign of the fact its digits ground to.

### Causes (rule 6)

This rule applies only when the bullet cites a segment fact (`LAP`, `T*`, `STR*`, `SEC*`).
"Because the tyre model was unavailable" is about data, not lap time.
- **Markers:** because (of), due to, caused by, as a result of, thanks to, owing to, stems
  from, attributable/attributed to, explained by, explains, led/leads to, resulting from, down
  to, the reason, contributing factor, related/linked to.
- **Negation:** a marker within five words of a negation ("no loss is attributed to") is not a
  claim.
- **What a claim must do:** name a cause keyword. Direction-specific groups are matched first:
  "lower minimum speed" means `low_min_speed`, "a faster exit" means `higher_exit_speed`. The
  generic groups ("braking", "the exit") come after.
- **What fails:** a claim fails if the cited segment's causes do not include the group, if the
  segment's only cause is `unclear`, or if it names an unsupported cause (tyres, traffic,
  wind, setup, fuel, mistakes, DRS, grip, wear, …).
- **Hedges:** a hedged claim ("may be a contributing factor") without a cause keyword is
  allowed; an unhedged one is not.

### Scope (rule 8)

Categories, each a regex listed in `validator.py`:
- **injury:** injury, hurt, pain, medical, …
- **politics:** team orders, contracts, favouritism, …
- **rules:** penalties, stewards, disqualification, illegal, …
- **intent:** scared, frustrated, tried to, deliberately, felt, …
- **guarantee:** must pit, pit now, guaranteed, definitely, certainly, …

`strategy_options` bullets must contain a hedge: indicative, suggests, may, might, could,
consider, possible, heuristic, likely, potential.

## Corpus

75 hand-labelled cases: 30 good and 45 bad, against four real FactSheets from the 7A fixtures.
- **Good:** correct roundings, citations, null handling, hedged causal statements.
- **Bad:** each lists `expect` (the rule), optionally `detail` (a substring of the violation)
  and `expect_bullet`. **Every rule has at least two bad cases and is exercised by at least
  one good case.**
- **Harness:** a case gives `section` + `bullet` (dropped into a minimal valid report), a
  `report_patch`, or a full `report` (`$HASH` is replaced by the FactSheet hash).

All good cases pass. Every bad case fails with its expected rule, and the test asserts that
specific violation, not just failure.

### False-positive log

Good cases marked `fp_guard` record phrasings a naive rule would reject. Add one whenever a
false positive is found.

| Case | Phrasing kept valid |
|---|---|
| g09 | "ignores undercut and traffic" — naming traffic without a causal claim |
| g10 | "carrying more minimum speed could recover…" — prescriptive advice, checked with the inverted sign |
| g12 | "no time loss is attributed to traffic" — negated attribution |
| g17 | "Straight 1 (0-475 m)" — digits inside a segment label |
| g22 | "because the tyre model was unavailable" — causal word about data, not about lap time |

Found while building the corpus: masking the label "Turn 2" consumed the "2" of "Turn 23" and
left a stray "3". Masks now match whole occurrences only (regression test in
`test_ai_numbers.py`).

## Mutation test

`python -m scripts.validator_mutation_test` mutates every good case (seeded):
- **digit:** change one digit of a real number, up to 3 per number;
- **sign:** flip a direction word, e.g. lost↔gained, slower↔faster, later↔earlier,
  less↔more, lower↔higher, cost↔saved;
- **unit:** s↔m, km/h→m, s/lap→km/h, %→s.

`test_mutation_kill_rate_is_at_least_98_percent` enforces the threshold.

| Run | Mutants | Killed | Kill rate |
|---|---|---|---|
| First run | 201 | 189 | 94.0% |
| After fixing the rule gaps below | 200 | 198 | **99.0%** (digit 100%, unit 100%, sign 93.5%) |

### Survivors of the first run, and what changed

| Survivor | Cause | Fix |
|---|---|---|
| "Turn 13 **saved** about 0.2 s", "T19 and T13 together **saved** …", "Turn 19 **saved** 0.311 s" (×2 more) | "saved" and "advantage" were not in the lexicon | added as time words (negative) |
| "the **higher** minimum speed may be a contributing factor" (Turn 19 lists `low_min_speed`); "a **faster** exit may be …" (Turn 13 lists `lower_exit_speed`) | cause keywords had no direction | direction-specific cause groups, matched before the generic ones |
| "carrying **less** minimum speed could recover…" | prescriptive phrasing was not checked | prescriptive entries with the inverted sign |
| "14 lap**m** old" | bug in the mutation script (it swapped the "s" of "laps" as a unit) | the unit swap only matches whole units |

### Remaining survivors (accepted: equivalent mutants)

| Survivor | Why it is not a defect |
|---|---|
| g19: "BOT **gained** time in 7 of 16 corners and gained in 7" | corners lost and corners gained are **both 7** in this FactSheet, so the mutant is still true |
| g19: "BOT lost time in 7 of 16 corners and **lost** in 7" | same reason |

The test allows survivors only from g19. Any new survivor fails it and must be analysed here.

## Known limitations

- **Perspective:** reports must describe the target. "PIA was faster by 0.311 s" is a correct
  fact phrased from the reference's side and fails `direction_mismatch`. The 7D prompt states
  the convention, and the retry explanation repeats it.
- **Negation:** only negated *causal* markers are recognised. "LEC did not lose time" with a
  positive loss is not caught; the corpus has no such case yet.
- **Counts:** direction words next to count facts ("lost time in 7 corners") are not checked
  (see the survivors above).
- **Sentences:** boundaries are `;`, `!`, `?` and a period not followed by a digit.

## Retry explanation

`explain_for_retry(violations)` returns at most 12 deduplicated lines of the form
`- section[i]: rule — detail`, plus one hint per rule family. It quotes the offending text, but
never lists FactSheet values or meanings (tested).

## Performance

A full seven-section report validates in well under 20 ms. That is tested, with the median of
30 runs after warm-up. The FactSheet index is cached by `factsheet_hash`.
