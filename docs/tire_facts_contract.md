# Tire facts contract (Phase 6F → Phase 7)

These are the exact tyre values a Phase 7 FactSheet may contain. Phase 7A builds directly from
this document. Every fact is read from **one** `GET /api/v1/tires/predict` response, the
`TirePrediction` schema in `backend/openapi.json`, for one (session, driver, "as of" lap).
No fact may be derived from laps after that lap. The API guarantees this, and
`test_changing_laps_after_the_cut_leaves_the_output_unchanged` guards it.

`backend/tests/test_tire_facts_contract.py` parses the two tables below and diffs them
against the API schema in both directions:
- every **Source** path must exist in `TirePrediction`;
- every `TirePrediction` field must appear as a source, or in "Not exposed as facts" with a
  reason.

A schema change that this document does not follow fails the backend suite.

## Rules for every fact

- **Null semantics:** a null source value gives a null fact. A fact is **never** zero-filled,
  defaulted or carried over from another lap. The FactSheet keeps the null and may cite the
  matching entry of `warnings` (or `reason` / `pit_window_reason`) as the explanation.
- **Rounding** applies when the FactSheet renders a value. The stored fact keeps the API value,
  which is already rounded to at most 3–4 decimals by the service.
- **Status gate:** when `status` is `insufficient_data`, only the identity, status and stint facts
  exist. Every forecast fact (degradation, window, predictions) is null by definition, and
  `reason_code` says why.
- **Labelling:** the pit window is a heuristic (`interval_crossing_v2`), not a team rule. Its
  assumptions must travel with any window fact. Degradation is a *combined adjusted trend* (wear
  plus residual track evolution after the fuel adjustment), never "pure tyre wear".

## Facts

| ID | Source | Unit | Rounding | Null semantics |
|---|---|---|---|---|
| `tire.session_key` | `session_key` | – | as is (`2024-14-R`) | never null |
| `tire.driver` | `driver` | – | as is (3-letter code) | never null |
| `tire.as_of_lap` | `lap` | lap | integer | never null; every other fact is "as of" this lap |
| `tire.horizon_laps` | `horizon` | laps | integer | never null |
| `tire.status` | `status` | – | `ok` \| `insufficient_data` | never null |
| `tire.reason_code` | `reason_code` | – | enum as is | null when status is `ok` |
| `tire.reason` | `reason` | – | as is | null when status is `ok` |
| `tire.clean_laps_required` | `clean_laps_required` | laps | integer | never null |
| `tire.race_laps_total` | `race_laps_total` | laps | integer | null if the race distance is unknown |
| `tire.stint` | `stint` | – | integer | null when FastF1 has no stint for the lap (`reason_code` = `unknown_stint`) |
| `tire.compound` | `compound` | – | upper-case name (`MEDIUM`) | null with `unknown_stint` |
| `tire.tyre_age_laps` | `tyre_age` | laps | integer | null with `unknown_stint` |
| `tire.clean_laps_used` | `clean_laps_used` | laps | integer | null with `unknown_stint` |
| `tire.rainfall_in_stint` | `rainfall_in_stint` | – | boolean | null with `unknown_stint` |
| `tire.deg_s_per_lap` | `degradation_s_per_lap` | s/lap | **3 dp** | null when not ok, or when the model has no degradation term (warning says so) |
| `tire.deg_ci_low` | `degradation_ci[0]` | s/lap | 3 dp | null when the CI is unavailable (warning says so) |
| `tire.deg_ci_high` | `degradation_ci[1]` | s/lap | 3 dp | as `tire.deg_ci_low` |
| `tire.next_lap_pred_s` | `predicted_laps[0].pred_s` | s | 3 dp | null when not ok |
| `tire.next_lap_lo_s` | `predicted_laps[0].lo_s` | s | 3 dp | 80% interval low; null when not ok |
| `tire.next_lap_hi_s` | `predicted_laps[0].hi_s` | s | 3 dp | 80% interval high; null when not ok |
| `tire.window_earliest_lap` | `pit_window.earliest_lap` | lap | integer | null when there is no window (see `tire.window_reason`), or when nothing crosses within the horizon |
| `tire.window_central_lap` | `pit_window.central_lap` | lap | integer | null when the forecast never reaches the threshold within the horizon |
| `tire.window_latest_lap` | `pit_window.latest_lap` | lap | integer | null = "no crossing within horizon" (open-ended window), **not** "unknown" |
| `tire.threshold_s` | `pit_window.threshold_s` | s | 1 dp | the X in "best pace + X"; null when there is no window |
| `tire.window_pace_basis` | `pit_window.pace_basis` | – | enum as is | null when there is no window |
| `tire.confidence` | `pit_window.confidence` | – | `high` \| `medium` \| `low` | null when there is no window |
| `tire.confidence_reasons` | `pit_window.confidence_reasons` | – | list of strings, as is | null when there is no window |
| `tire.window_assumptions` | `pit_window.assumptions` | – | list of strings, as is | null when there is no window; must accompany any window fact |
| `tire.window_method` | `pit_window.method` | – | as is (`interval_crossing_v2`) | null when there is no window |
| `tire.window_reason` | `pit_window_reason` | – | as is (e.g. `forecast_uncertainty_too_high`) | null when a window exists or status is not ok |
| `tire.remaining_competitive_laps` | `remaining_competitive_laps` | laps | integer | null when `tire.window_central_lap` is null |
| `tire.model_name` | `model.name` | – | as is | null only if no model answered |
| `tire.model_version` | `model.version` | – | as is | as `tire.model_name` |
| `tire.model_trained_through` | `model.trained_through` | date | ISO `YYYY-MM-DD` | as `tire.model_name` |

## Not exposed as facts

These response fields support the panel but are not FactSheet facts.

| Field | Reason |
|---|---|
| `predicted_laps` | the full per-lap curve is chart data; only the next lap is a fact (above) |
| `history` | observed laps of the stint are chart data, already summarised by `tire.clean_laps_used` |
| `threshold_curve` | per-lap threshold on the raw scale, for drawing only; the fact is `tire.threshold_s` |
| `warnings` | explanations of nulls, cited by the FactSheet rather than stated as facts |
| `pit_window.threshold_pace_s` | absolute pace on the model's fuel-adjusted basis, not comparable to raw lap times |
| `pit_window.best_pace_s` | same basis caveat as `threshold_pace_s` |
| `pit_window.notes` | free-text qualifiers; their content is covered by `tire.window_latest_lap` null semantics and `tire.window_reason` |
