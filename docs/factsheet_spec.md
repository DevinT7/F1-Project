# FactSheet specification (Phase 7A)

The FactSheet is the **only** input of the AI race engineer. It is built from the Phase 3
verified findings, the Phase 6E/6F tyre response, and a little context. The LLM never sees
telemetry arrays and has no tool that computes numbers. Every number a report may contain is a
fact listed here, with the exact digit strings it may be written as.

Code: `backend/app/ai/` (`factsheet.py`, `schemas.py`, `numbers.py`). Tests:
`tests/test_ai_factsheet.py` and `tests/test_ai_numbers.py`. Fixtures: `tests/fixtures/ai/`.
Rebuild them from ingested sessions with `python -m scripts.build_factsheet_fixtures`.

```python
from app.ai.factsheet import build_factsheet, FactSheetContext
fs = build_factsheet(findings_report, tire_prediction_or_None, FactSheetContext(session_date=..., teams=...))
```

## Structure

| Section | Content |
|---|---|
| `factsheet_version` | `"1"` |
| `session` | `key` (`2024-14-R`), `event`, `session_type` (FastF1 code: Q, R, …), `session_name`, `date` |
| `drivers` | `target` and `reference`: `code`, `team`, `lap`, `lap_time_s` |
| `lap_summary` | `total_delta_s`, `sector_deltas_s` (3), `top_speed_diff_kph` |
| `segments` | per corner, straight and sector: `label`, `fact_prefix`, `kind`, `time_loss_s`, `confidence`, `causes`, `rank` |
| `rankings` | `largest_losses` and `largest_gains` as fact prefixes, biggest first |
| `tire` | the tyre contract values keyed without `tire.`, plus `status`; or `{"status": "unavailable", "reason": "…"}` |
| `conditions` | `track_temp_c`, `air_temp_c` (target lap), `track_status_flags` (both laps), `traffic_data: "unavailable"` |
| `data_quality` | `low_confidence_segments` (prefixes), `notes` (whole-comparison, context and per-segment notes, the latter prefixed `T6: …`) |
| `facts` | every numeric value, see below |
| `categorical_facts` | every non-numeric value: labels, codes, causes, confidences, flags, notes |
| `factsheet_hash` | SHA-256 of the canonical JSON of everything above |

The summary sections only repeat values that are also facts. **`facts` is authoritative.**

## Facts

```json
{"id": "T6.time_loss_s", "value": 0.0983, "unit": "s", "display": ["0.098", "0.10", "0.1"],
 "meaning": "time the target lost to the reference over Turn 6 (positive = target slower, negative = faster)",
 "source": "findings:/corners/5/time_loss_s"}
```

- **`id`** is `{prefix}.{field}`. Prefixes:
  - `LAP`, `T1`…`Tn` (corners), `STR1`… (straights), `SEC1`…`SEC3` (sectors)
  - `target`, `reference`, `session`, `quality`, `rankings`, `data_quality`, `tire`

  Segment fields:
  - `time_loss_s`, `entry_loss_s`, `exit_loss_s`, `rank`, `corner_number`, and
    `start_m` / `end_m` / `apex_m` for the span;
  - every metrics key (`braking_start_diff_m`, …);
  - per-driver absolute values as `target_<key>` / `reference_<key>` (`T6.target_min_speed_kph`).
- **`value`** is the source value, unrounded. It is `null` when unavailable.
- **`display`** holds the exact digit strings a report may use, primary precision first. They
  are magnitudes only: a sign is expressed in words ("lost", "faster") or by a written sign that
  matches the value's sign (checked in 7B).
- **`meaning`** says what the number is and its sign convention **in words**, e.g. "positive =
  target started braking later". When the value is null it starts with `unavailable — ` and gives
  the reason, e.g. "unavailable — no braking detected in this segment".
- **`source`** is a JSON pointer into the findings (`findings:/…`) or the tyre response
  (`tire:/…`). It is `derived:…` when no source value exists, for example when there is no tyre
  forecast at all.

### Display forms: `display_forms(value, unit)`

| Unit | Primary | Also allowed |
|---|---|---|
| `s` | 3 dp | 2 dp, 1 dp |
| `s/lap` | 3 dp | 2 dp |
| `km/h`, `m`, `°C`, `m/s²`, `%` | 1 dp | 0 dp |
| `ratio` | 4 dp | 3 dp, 2 dp |
| `fraction` (0–1) | 2 dp | 1 dp |
| `lap`, `laps`, `count`, `gear`, `rank`, `corner` | integer | — |

A coarser form is kept only if it is a correct rounding of the value **and** does not turn a
non-zero value into zero. For example, 0.004 s gives only `["0.004"]` and 0.049 s gives
`["0.049", "0.05"]`. A null gives `[]`; a null is never rendered as "0".

### Nulls are facts

A missing value keeps its fact, with `value: null`, `display: []` and a meaning beginning
"unavailable —", so the AI can say *that* it is missing instead of inventing it. The reason is specific:

- **Differences say which side is missing.** For example, "no braking detected for the reference
  (PIA) only; the target (LEC) has a value". It never reads as if neither driver braked.
- **Tyre nulls follow the contract.** `tire.window_latest_lap` null means "no crossing within
  the forecast horizon: the window is open-ended … this is not missing data". `tire.reason_code`
  null means "not applicable: the forecast is available". With `insufficient_data`, every
  forecast fact gives the service's reason. When there is no
tyre forecast at all, every `tire.*` fact is present and null, with the reason (e.g. "tyre
forecasting requires a race session; this is qualifying").

### Categorical facts

```json
{"id": "T6.causes", "value": ["late_braking_gain", "late_throttle"], "allowed": ["early_braking_loss", "…"],
 "meaning": "triggered rules (contributing factors, not proof); the only causes a report may name",
 "source": "findings:/corners/5/causes"}
```

Labels, driver codes, compounds, confidences, causes, flags, notes and the tyre window's
assumptions are categorical. **`T*.causes` is the closed list of causes a report may attribute a
loss to.** Causal language beyond it is rejected in 7B.

## Mapping guarantees (tested)

- **Coverage.** Every leaf of the source findings JSON and tyre JSON maps to exactly one fact
  (numbers to numeric facts, strings, bools and string lists to categorical facts, nulls to
  exactly one fact), or is an explicit exclusion:
  - *Findings* (`FINDINGS_EXCLUDED`): `schema_version`, `comparison_id`, and each finding's
    `id` / `kind`. The fact prefix *is* the identity.
  - *Tyre* (`TIRE_NOT_EXPOSED`): exactly the "Not exposed as facts" table of
    `docs/tire_facts_contract.md`. That covers the per-lap curve beyond the next lap (whose lap
    number is always `as_of_lap + 1`), `history`, `threshold_curve`, `warnings`, the window's
    fuel-adjusted `threshold_pace_s` / `best_pace_s`, and `notes`.
- **No unexplained fields.** A metric key, per-driver value or quality field without an explicit
  meaning makes the builder raise. There is no generic fallback, so a new findings field
  cannot reach the AI unexplained.
- **Contract sync.** The tyre fact table in code equals the Facts table of
  `docs/tire_facts_contract.md`: same IDs, same sources.
- **Hash.** `factsheet_hash` is stable for equal inputs and changes when any value changes.

## Number extraction: `parse_numbers(text)`

The validator (7B) uses `parse_numbers` to find every number in generated text. Each token
carries its signed value, magnitude as written, sign, normalized unit, kind
(`number`, `laptime`, `ordinal`, `word`) and position.

- **Units** (attached or spaced): `s`/sec/seconds, `s/lap`, `km/h`/kph, `m`/metres/meters,
  `°C`, `m/s²`, `%`/percent, laps.
- **Signs:** ASCII `-`, Unicode `−`, `+`. A dash between two numbers (`0.05–0.13`, `0.05-0.13`)
  is a range, not a minus.
- **Thousands separators** (`1,234.5 m`), decimals without a leading zero (`.5 s`), and lap times
  (`1:33.660` gives 93.66 s).
- **Labels are not numbers:** `T7`, `S2`, `STR1`, `Turn 7`, `Sector 2`, `Straight 3`, `stint 2`,
  `gear 3`, `#44`.
- **`lap 24` is a number** (unit `lap`), because lap numbers are facts (pit-window laps).
- **Number words.** Only `one`, `two`, `three`, `first` and `second` are allowed, the latter
  as an ordinal ("the second sector"). "a second", "half a second" and "0.5 second" are time
  quantities and are rejected. Every other spelled-out number or fraction word (`seven`, `tenths`,
  `half`, `third`, …) is a token flagged `allowed=False`.
- **Masking:** `ignore=(...)` masks literal identifiers (session key, event name) before
  parsing.

The parser table in `tests/test_ai_numbers.py` has 48 cases, including every example from the
plan.

## Fixtures

| Fixture | Case |
|---|---|
| `q_alo_vs_ver_fastest` | qualifying, fastest laps; no tyre forecast (non-race) |
| `q_bot_vs_alo_no_braking` | qualifying; corners with no braking detected (null braking facts), low-confidence corners |
| `race_lec_pit_window` | Spa 2024 race, LEC lap 39 vs PIA; tyre forecast with a pit window |
| `race_alo_tire_model_unavailable` | race; tyre model unavailable (HTTP 503) |
| `race_lec_tire_insufficient` | race, LEC just pitted; tyre `insufficient_data` |

Each fixture is `<name>.inputs.json` (findings, tyre and context) plus `<name>.factsheet.json`
(the builder's output, a golden file).

## Size, and what it means for 7D

A full FactSheet holds about 860–1,000 numeric facts and 190–210 categorical facts, around
300 KB of JSON. Most are per-driver absolute values and segment spans that a report rarely
needs. The FactSheet is the complete catalogue of what *may* be said. **7D/7E should send the
model a compact rendering, or the relevant slices,** not the whole document. The validator still
checks against the full FactSheet.
