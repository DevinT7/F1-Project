# RaceIQ project plan addendum

This project already implements Phases 0–9, including lap comparison charts, a basic summary, tire analysis, validated AI engineer, observability, and CI/CD. The current checkout is the source of truth; continue phase by phase and record corrections in `docs/progress.md`.

## Data ingestion scope

Backfill completed sessions from 2018 onward for FP1, FP2, FP3, qualifying, and race where FastF1 provides valid data. Preserve validity flags and report unavailable sessions rather than treating them as ingestion failures with fabricated data. The season CLI defaults to these five session codes. Store data in the configured local/object store; do not commit generated telemetry blobs.

## AI engineer credentials

The engineer remains FactSheet-only, validated, and template-fallback capable. A free hosted provider still requires an account and user-owned API key. Never search for, invent, commit, or reuse a third-party secret. Configure providers via environment variables and keep CI on fake LLMs.

## Phase 10

Take one stretch at a time. Start with user-uploaded sim telemetry; its design, spec, tests, and acceptance criteria are in [docs/stretch/user-uploaded-telemetry.md](docs/stretch/user-uploaded-telemetry.md). Other stretch features remain unselected until this one meets acceptance.

## Permanent test invariants

- `delta_s[-1]` equals target lap time minus reference lap time within 0.02 s.
- Segment time losses sum to the final delta.
- Swapping target and reference negates every delta; self-comparison yields zero.
- Every number in an AI report exists in its FactSheet; fallback reports pass validation.
- API responses contain no NaN or infinity.

See the phase test strategy and risk register in [docs/progress.md](docs/progress.md) and [docs/limitations.md](docs/limitations.md).

## Appendix A — Cross-phase test strategy

| Layer | Tooling | What it proves |
|---|---|---|
| Pure numerical | pytest + hypothesis + synthetic generator | Formulas are right and invariants hold |
| Data adapters | Fixtures + network-marked integration | FastF1 assumptions still hold |
| API | httpx + fixture repo | Contracts, errors, performance |
| ML | Synthetic races + leave-one-race-out | No leakage and honest metrics |
| AI | Fake LLM + validator corpus | No ungrounded numbers reach users |
| UI | Vitest + MSW + Playwright | Interactions, sign conventions, accessibility |
| Ops | Testcontainers, compose E2E, load tests | The deployed system behaves |

Permanent invariants: final delta matches target minus reference lap time (±0.02 s); segment losses sum to final delta; swapping laps negates every delta; self-comparison is zero; every AI report number exists in its FactSheet; fallback reports pass validation; API responses contain no NaN/Inf.

## Appendix B — Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| FastF1 gaps or API changes | Broken ingestion | Adapter isolation, pinned version, validity flags, cached fixtures |
| Distance drift | False corner loss | Length normalization and final-delta integrity check |
| Tire/fuel/track-evolution confounding | Misleading degradation | Fuel proxy, pooled fixed-effects model, honest reporting |
| LLM invents values or causes | Loss of trust | FactSheet-only input, validator, fallback |
| Tire model overfit | Bad predictions | Leave-one-race-out, walk-forward, interval calibration |
| Scope creep | Never finishes | Keep MVP complete; one stretch feature at a time |
| Data/licensing issues | Legal exposure | Use FastF1 as intended, avoid official branding, document limits |

## Appendix C — First-session checklist

1. Keep this plan at the repository root and review `docs/progress.md` before each phase.
2. Start with the current phase's kickoff prompt/spec; after it passes `make check`, commit code and plan updates together.
3. Correct the plan when implementation or upstream data behavior disproves an assumption.
4. Record data gaps and provider limitations; do not silently substitute synthetic results for real data.
5. Keep Phase 10 stretch work in its own design doc and complete its spec, tests, and acceptance criteria before selecting another stretch.
