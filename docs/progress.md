# Progress log

## Phase 2 — Lap alignment and delta engine (P1, 2026-10-12)

Code: `backend/app/processing/` (`alignment.py`, `delta.py`, `models.py`, `errors.py`).
The synthetic known-answer generator is `backend/tests/fixtures/synthetic.py`.
Entry point:

```python
from app.processing.delta import compare_laps, sector_check
from app.processing.models import LapData

ref = LapData.from_repository(repo, "2024_05_Q", "VER", 18)
tgt = LapData.from_repository(repo, "2024_05_Q", "ALO", 15)
cmp = compare_laps(ref, tgt, step=1.0)   # cmp.delta_s, cmp.speed_diff_kph, cmp.quality, ...
diag = sector_check(cmp)                 # independent sector-boundary cross-check
```

The algorithm is implemented as specified: clean, then linear length normalization,
then a 1 m grid, then linear interpolation for continuous channels and
previous-sample steps for gear, brake and DRS. The delta comes from recorded
timestamps; there is no 1/speed integration fallback. Smoothing is display-only.

### Acceptance

| Criterion | Result |
|---|---|
| Known-answer and antisymmetry tests | Pass. The delta matches the exact synthetic t(d) within 2 ms everywhere. Antisymmetry holds within 1e-6 s. |
| Real data: final-delta error ≤ 0.02 s for ≥ 95 % of valid pairs | **100 %** of 6000 pairs (3000 from 2024 R5 Q, 3000 from R). No comparison raised an error. |
| mypy strict, ≥ 90 % coverage on `processing/` | mypy is clean. Coverage is **98 %**. |
| Benchmark < 50 ms per comparison at 1 m | **~1.0 ms median, ~2.4 ms p95** (`python -m scripts.bench_alignment`, Python 3.11, Intel Family 6 Model 189 laptop). |

### Final-delta error distribution (real data, `python -m scripts.alignment_report`)

| Session | Valid laps | Pairs | Median \|error\| | p95 | Max |
|---|---|---|---|---|---|
| 2024_05_Q | 92 | 3000 | 0.000000 s | 0.000000 s | 0.000000 s |
| 2024_05_R | 757 | 3000 | 0.000000 s | 0.000000 s | 0.000000 s |

That every error is exactly zero is expected, and it's worth knowing why: FastF1's
`Lap.get_telemetry()` interpolates the first and last samples onto the lap start and
end. So every lap's telemetry duration equals its official `LapTime` (checked on all
92 valid Q laps). On FastF1 data the final-delta check therefore guards against bugs
in our own interpolation, not against data problems. It still catches broken
telemetry and timing (tested with synthetic laps).

### Known limitation: mid-lap accuracy is about ±40–60 ms, not ±0.02 s

The sector cross-check is the real independent test. It **fails most of the time on
real data**:

| Session | Sector-boundary \|discrepancy\| median | p95 | All boundaries within 0.03 s |
|---|---|---|---|
| 2024_05_Q | 0.041 s | 0.136 s | 16 % of pairs |
| 2024_05_R | 0.064 s | 0.195 s | 9 % of pairs |

Here's the diagnosis. The physical sector lines sit at different FastF1 `Distance`
values from lap to lap. The standard deviation is 5–8 m raw, and still 3.7–5.6 m after
linear length normalization. At ~70 m/s that's 50–80 ms. Same-driver pairs show it
too (median 34–48 ms), so it isn't racing-line differences. It's speed-integration
drift that varies along the lap. A single linear scale factor corrects it only at the
finish line, which is exactly where we check.

Here's what that means downstream. The delta curve's **shape** is reliable (synthetic
tests). Its **endpoint** is exact. Its **mid-lap absolute value** carries tens of ms of
position-alignment uncertainty. Corner-level time-loss numbers in later phases
should be reported with that uncertainty, or with sector deltas as the anchor.

Options considered (not implemented, pending a decision):
- **Anchor the normalization at the sector lines.** Use piecewise-linear normalization
  through each lap's own S1/S2 line distances, from its own official sector times. In a
  held-out test (anchor S1, measure at S2) the race median fell from 59 ms to 38 ms and
  Q from 44 ms to 40 ms. It's cheap, but the gain is modest, and the sector check
  becomes true by construction.
- **Align on X/Y position instead of integrated distance.** Project each sample onto
  the reference lap's path. This attacks the cause, but it's a larger change.

### Other notes

- **Resampling invariance** holds within 0.5 km/h and 5 ms between 10 Hz and 25 Hz
  input. At 4 Hz the samples are ~21 m apart at 315 km/h, and linear speed
  interpolation through a 315→95 km/h braking zone is off by up to ~2.5 km/h. That's
  a limit of sampling, not of alignment. FastF1 merged telemetry is ~7.5 Hz.
- **Failure handling.** Duplicate timestamps and unsorted samples are cleaned. A
  non-advancing distance at > 5 km/h is dropped as a glitch (counted in
  `quality.dropped_glitches`), and stationary samples get an epsilon nudge (counted in
  `quality.repairs`). These raise errors instead: fewer than 200 usable samples or a
  duration off by more than 0.15 s (`LapInvalidError`), a length mismatch over 3 %
  (`LapInvalidError`), and a final-delta mismatch over 0.02 s (`InsufficientDataError`).
  On the real data, no repairs or glitch drops were needed.
- Smoothed `delta_rate_smoothed` (Savitzky–Golay, 25 m, order 2) is for display only.
  `delta_rate` (the unsmoothed gradient) is kept for any numeric use.
