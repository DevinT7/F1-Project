# Stretch 10.1 — User-uploaded sim telemetry

**Status:** selected first Phase 10 stretch feature; design and acceptance contract. Implementation is not yet complete.

## Spec

Allow a user to upload a supported simulator CSV lap, normalize it to the existing `TelemetryFrame`, and compare it with a reference lap only when both identify the same track. First supported parser: a documented generic CSV export with time, distance, speed, throttle, brake, gear, and optional X/Y columns. F1 game UDP capture is a later adapter behind the same normalized format.

The parser must run in a bounded worker/process with no network or filesystem access. Enforce a 10 MiB file limit, UTF-8 text input, row/sample limits, finite numeric values, monotonic time, non-decreasing distance, and a minimum usable sample count. Reject unsupported or malformed files with actionable errors. Never retain the original upload after the comparison request.

Comparisons must display explicit warnings for mismatched track length (over 3%), tire model, and sampling rate. Unknown metadata must be shown as unknown; it must not be inferred from telemetry. Existing distance alignment and final-delta integrity checks remain authoritative. Simulator and real-world tire behavior must not be treated as equivalent.

## Tests

- Parser fixtures for valid CSV, alternate headers, missing optional channels, malformed encodings, oversized input, excessive rows, NaN/Inf, duplicate timestamps, distance reversals, and too few samples.
- Property tests for normalized frame invariants and no NaN/Inf output.
- API tests for upload limits, unsupported formats, comparison rejection on track mismatch, warning generation for track/tire/sampling differences, and upload non-retention.
- Frontend tests for accessible file selection, progress, errors, metadata warnings, and clearing a selected file.
- E2E test with an in-memory fixture upload; no network calls or persistent file writes.

## Acceptance criteria

1. Valid supported CSV uploads normalize to `TelemetryFrame` and render through the existing comparison chart.
2. Invalid, oversized, or resource-exhausting inputs fail before comparison with a useful error and do not crash the API.
3. Comparisons are blocked when track identity is missing or different.
4. Track-length, tire-model, and sampling differences are visible before interpreting results.
5. Uploaded bytes are not persisted, logged, included in AI FactSheets, or sent to an LLM.
6. Numerical invariants remain in force: final delta matches the official lap-time difference when available, segment losses sum to the final delta, and no API response contains NaN/Inf.

## Mini-plan

1. Implement and test the isolated CSV parser and normalized metadata schema.
2. Add bounded upload API handling and matching-track comparison checks.
3. Add the upload UI and explicit assumption/warning panel.
4. Run parser, API, frontend, and E2E acceptance suites; document supported CSV headers and limitations.
5. Consider UDP capture only after CSV is accepted and licensed data sources are reviewed.
