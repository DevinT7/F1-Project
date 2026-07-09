# Offline fixtures — session 9159 (2023 Singapore GP, Race)

Saved OpenF1-shaped JSON for two drivers so `telemetry.py` / `analysis.py` can be
built and tested with the network off (Phase 0 of `P2_ANALYSIS_PLAN.md`).

**Provenance note:** this sandbox has no outbound access to `api.openf1.org`, so
these files are a *synthetic reconstruction* of the real endpoint shapes, not a
scrape of live data — field names, types, and nesting match the real OpenF1
`sessions` / `drivers` / `laps` / `car_data` responses exactly, and the telemetry
was generated from a plausible Marina Bay-style speed profile (19 corners, ~5.06 km,
low top speed, heavy braking zones) so corner detection and delta math have
something realistic to chew on. Swap in real captured JSON later by regenerating
these files with the same names/shapes — nothing downstream needs to change.

Drivers used: `55` (Sainz, Ferrari) and `4` (Norris, McLaren) — the real front two
finishers of that race — with fictional-but-plausible sample telemetry.

## Files
- `sessions.json` — one `sessions` row for `session_key=9159`.
- `drivers.json` — `drivers` rows for driver 55 and driver 4.
- `laps.json` — one flying-lap `laps` row per driver (lap_number=18), with sector
  durations and `date_start`.
- `car_data_55.json` / `car_data_4.json` — time-sampled `car_data` rows covering
  each driver's lap window (dt=0.2s, ~speed/throttle/brake/n_gear/rpm/drs).

## Regenerating
`gen_fixtures.py` in this directory produces all of the above deterministically
(`python3 gen_fixtures.py`). Edit the corner list / speed profile there to try
different circuits or edge cases (pit-out lap, missing channels, short lap, etc.).

## Using them
`fake_openf1.py` exposes `FakeOpenF1Client`, a drop-in for
`app.clients.openf1.OpenF1Client` that reads these files instead of hitting the
network — same method names (`sessions`, `drivers`, `laps`, `car_data`, ...), same
filtering semantics (`driver_number`, `date_gt`/`date_lt` on `car_data`). Point
`app.services.telemetry.get_client` / `app.services.sessions.get_client` at it via
monkeypatch in tests. See `backend/tests/test_offline_compare.py`.
