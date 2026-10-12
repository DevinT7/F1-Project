# Phase 5 visual sanity checklist

These screenshots come from the production build (`next build --webpack`) talking to the
backend, which serves the Parquet fixture repository (2024 R5 Qualifying, VER vs ALO,
fastest valid laps). They were captured with Playwright on 2026-10-12.

| # | File | What to check | Result |
|---|---|---|---|
| 1 | `01-dashboard-dark.png` | Dark theme (default). T6 is selected: the chart band, the map ring and the corner panel all point at Turn 6. The hover cursor shows in all rows, and the map marker sits at the same distance. | ✅ |
| 2 | `02-dashboard-light.png` | Light theme, which is the original pit-wall palette. With nothing selected, the corner panel shows the largest loss (Turn 16). | ✅ |
| 3 | `03-mobile-dark.png` | 390 px wide: everything stacks into one column with no horizontal scroll, and the charts still render. | ✅ |
| 4 | `04-demo-mode.png` | `?demo=1` shows the demo banner and draws the comparison with no backend calls. | ✅ |
| 5 | `05-not-ingested.png` | `?s=2024-06-Q` (not stored): the Ingest prompt appears, with a time estimate. | ✅ |
| 6 | `06-error-toast.png` | `tgt=ALO:1` (a lap with no lap time): an inline error, plus a toast showing `code: lap_invalid` and the `request_id`. | ✅ |

**Spot check: map colours against finding signs** (from screenshot 1 and the findings list)

| Corner | Finding `time_loss_s` | Map / chip |
|---|---|---|
| T16 | +0.171 s (ALO lost) | red, ▲ |
| T11 | −0.094 s (ALO gained) | green, ▼ |
| T9 | −0.011 s (inside ±0.02 s) | grey, ● |

The same three corners are asserted automatically in
`src/components/dashboard.test.tsx` ("map colours agree with the findings' signs").
