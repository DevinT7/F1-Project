# Template report (Phase 7C)

`build_template_report(factsheet, max_words=450) -> Report` builds the AI race engineer's report
with **no LLM**. It returns the same `Report` schema an LLM must produce, with
`generated_by="template"` and `prompt_version="template-v1"`, and it always passes the 7B
validator. It has three roles:
1. **The safety net:** in 7D, any LLM failure (timeout, invalid output after retries) degrades
   to this report, never to unchecked text.
2. **A product on its own:** it is correct and clear, if not varied.
3. **The UI's development backend:** the 7F stub endpoint serves it.

Code: `backend/app/ai/fallback.py`. Tests: `tests/test_ai_fallback.py`; the random-FactSheet
strategy is in `tests/ai_strategies.py`. Print the reports: `python -m scripts.print_template_reports`
(add `--write` to refresh the snapshots after a deliberate change).

## Rules every sentence follows

- **Numbers:** every number is `display[0]` of a fact cited in the same bullet. The only
  exceptions are text quoted verbatim from the FactSheet (the tyre "unavailable" reason), which
  the validator masks.
- **Directions are words:** "braked 18.0 m earlier", "lost", "faster", "lap times improve by". A
  sign is never written in prose.
- **Metrics:** a segment is described only through the metrics behind its triggered causes. The
  cause picks the metric; **the metric's sign picks the word**, so the text cannot contradict
  the data even if a cause and a sign ever disagreed.
- **No causal connectives** (no "because" or "due to"). Metrics are stated next to the loss, and
  the findings' causes stay "contributing factors".
- **Target perspective:** the report describes the target. Suggestions cite what the reference
  did, with the reference as the grammatical subject; the 7B subject rule checks that.
- **Nulls:** a null fact is cited only with an explicit availability phrase, never a number.
- **Counts that are not facts** (e.g. "one other segment") use the allowed number words, never
  digits.

## Template library

### Headline

| Case | Text |
|---|---|
| lap delta > 0 | `{T} was {Δ} s slower than {R} over the lap and lost the most in Sector {n} ({δ} s).` |
| lap delta < 0 | `{T} was {Δ} s faster than {R} over the lap and gained the most in Sector {n} ({δ} s).` |
| Δ displays as 0.000 | `{T} and {R} set the same lap time (0.000 s apart).` |
| lap delta null | `The lap time difference between {T} and {R} is unavailable.` |

"Lost" or "gained" for the sector follows that sector's own sign. It can differ from the lap's
(race_alo: 0.033 s slower overall, but gained the most in Sector 2). The sector clause is omitted
when no sector delta exists.

### Segment fragments (biggest time loss / strongest areas)

`{label}: {T} lost|gained {loss} s ({fragment}; {fragment}).` uses the top 3 of
`rankings.largest_losses` or `largest_gains`. Each triggered cause contributes one fragment for
its metric:

| Causes | Metric | Positive value | Negative value |
|---|---|---|---|
| `early_braking_loss`, `late_braking_gain` | `braking_start_diff_m` | braked {d} m later | braked {d} m earlier |
| `low_min_speed`, `high_min_speed` | `min_speed_diff_kph` | carried {d} km/h more minimum speed | carried {d} km/h less minimum speed |
| `late_throttle`, `early_throttle` | `time_to_full_throttle_diff_s` | reached full throttle {d} s later | reached full throttle {d} s earlier |
| `lower_exit_speed`, `higher_exit_speed` | `exit_speed_diff_kph` | exited {d} km/h faster | exited {d} km/h slower |
| `straight_line_deficit`, `straight_line_advantage` | `top_speed_diff_kph` | reached a top speed {d} km/h higher | … lower |

The other cases:

| Case | Text |
|---|---|
| only `unclear` | `{label}: {T} lost {d} s; the time loss has no single clear driving cause in the available data.` (or "gain") |
| causes, but every metric behind them is null | `{label}: {T} lost {d} s.` |
| no losses | `No corner cost {T} time beyond the noise floor.` |
| no gains | `{T} did not gain time in any corner beyond the noise floor.` |

### Driving suggestions

One suggestion per top-loss segment: the first loss-type cause whose metric has the expected
sign. At most 3.

| Cause | Text |
|---|---|
| `early_braking_loss` | `Test a later brake point in {label}; {R} braked {d} m later.` |
| `low_min_speed` | `Work on minimum speed in {label}; {R} carried {d} km/h more minimum speed.` |
| `late_throttle` | `Aim for full throttle sooner out of {label}; {T} took {d} s longer to reach it.` |
| `lower_exit_speed` | `Prioritise the exit of {label}; {R} exited {d} km/h faster.` |
| `straight_line_deficit` | `Check straight-line speed on {label}; {R} reached a top speed {d} km/h higher.` |

### Tyre notes and strategy options

| Tyre state | Tyre notes | Strategy options |
|---|---|---|
| unavailable | `Tyre forecast unavailable: {reason}.` (reason quoted verbatim) | none |
| insufficient_data | `No tyre forecast yet: {used} of the {n} required clean laps in the current stint.` | none |
| ok, window | degradation, plus the window (open-ended / beyond-horizon wording for nulls) and its assumptions | `Indicative pit window: laps {e}–{l}, central lap {c} (confidence: {x}); it ignores traffic and safety-car risk.` (or "from lap {e} … open-ended beyond the forecast horizon") |
| ok, no window | degradation; `No pit window: {reason}.` | none |

Degradation reads `On the {compound} tyre, lap times grow by {d} s/lap (95% CI {lo} s/lap to
{hi} s/lap): a combined adjusted trend, not pure tyre wear.` A negative value says "improve by",
and a negative CI bound says "{d} s/lap of improvement". When rain was reported in the stint, a
bullet notes that the model is trained on dry races only.

### Data limitations (at most 5, in this order)

1. Traffic data is unavailable, so no time loss is attributed to traffic.
2. Listed causes are rule-based contributing factors, not proof.
3. When the sector cross-check failed: corner-level times carry up to {x} s of alignment
   uncertainty.
4. `Low confidence: {up to 4 labels}[ and one/two/three/several other segment(s)].`
5. For reported segments with no braking: `{label}: braking points cannot be compared; no braking
   was detected for {driver | either driver}.`

## Edge cases

| Case | Behaviour |
|---|---|
| target faster everywhere | no-losses sentence; the strongest areas list the gains |
| tied segments | follow the findings' ranking order (distance breaks ties) |
| all metrics null | loss without fragments; nulls appear only in data limitations |
| qualifying / tyre unavailable | the tyre reason is quoted, and there are no strategy options |
| word budget (`max_words`) | bullets are dropped lowest priority first (data limitations, strongest areas, driving suggestions, tyre notes, losses, strategy), keeping each section's first bullet until nothing else is left; the headline is never dropped (tested at 450, 200, 120 and 60 words) |

## Tests

- **Property:** 1,000 random valid FactSheets (`tests/ai_strategies.py`) all produce a report
  that passes the validator, within the 60-word bullet and 450-word report limits. The
  strategy builds findings the way the engine does: random metrics, signs and nulls, causes
  from the engine's own attribution functions, rankings from the losses, and every tyre state.
- **Fixtures:** all five fixture reports pass the validator. Their rendered text is
  snapshot-tested (`tests/fixtures/ai/<name>.report.json`).
- **Direction:** swapping target and reference (every difference negated) flips "slower" to
  "faster". LEC's biggest loss (Turn 19) becomes PIA's top gain, and the swapped report is valid.
- **Budgets and edge cases:** the no-losses wording, the qualifying and tyre-unavailable paths,
  and the budget trimming.

## Frontend

The report is the `schemas.Report` JSON: a headline, six bullet lists, and every bullet carrying
`text` + `fact_ids`. That is exactly what an LLM report looks like. The frontend renders both
the same way and can use `fact_ids` for hover details. 7F's stub endpoint serves this generator.
