# RaceIQ — Data Sources

Research summary for where RaceIQ gets its data. **Primary: OpenF1** (live + recent
historical, simple REST). **Secondary: FastF1** (richest distance-aligned telemetry +
older seasons). **Tertiary: Jolpica-F1** (schedules/results/standings). All three are
unofficial, community-run, and non-commercial — fine for a student project.

## 1. OpenF1 — https://openf1.org  (PRIMARY)
Open-source REST API. No key required for historical data.

- **Coverage:** 2023 → present. 18 endpoints.
- **Base URL:** `https://api.openf1.org/v1/`
- **Formats:** JSON (default), CSV (`?csv=true`).
- **Rate limits (free):** 3 req/s, 30 req/min. Sponsor tier (€9.90/mo): 6 req/s,
  60 req/min **+ live data** via REST/MQTT/WebSocket.
- **Live vs historical:** data is "live" from 30 min before a session to 30 min after;
  live streaming needs the sponsor tier. Historical is always free.
- **License:** CC BY-NC-SA 4.0 (non-commercial). Not affiliated with F1.

Key endpoints we use:
| Endpoint | Gives us |
|----------|----------|
| `sessions` | session_key, year, GP, session type — the picker |
| `drivers` | driver_number, name, team, colour, headshot |
| `car_data` | speed, throttle, brake(0/100), n_gear, rpm, drs @ ~3.7 Hz |
| `laps` | lap_duration, duration_sector_1/2/3, i1/i2/st_speed, is_pit_out_lap |
| `stints` | compound, tyre_age_at_start, lap_start/lap_end |
| `pit` | pit_duration, lap_number |
| `weather` | air/track temp, humidity, wind, rainfall |
| `location` | x/y/z track position (for the map + distance derivation) |
| `intervals`, `position`, `race_control`, `team_radio`, `overtakes`, `session_result`, `starting_grid`, `championship_*` | strategy/context |

Everything keys off `session_key` (use `latest` for the current session) and
`driver_number`. Numeric filters support operators, e.g. `?speed>=315`,
`?lap_number<=3`.

**Gotcha:** `car_data` is time-sampled, **not** distance-indexed. To compare two laps
"by distance" (project requirement) we must derive distance ourselves — integrate
speed over time, or use the `location` channel. FastF1 does this for us (below).

## 2. FastF1 — https://github.com/theOehrly/Fast-F1  (SECONDARY, telemetry-rich)
Python package (v3.8.x, MIT). Pulls the official F1 live-timing feed + Jolpica and
returns extended Pandas DataFrames, **with built-in caching**.

- **Coverage:** timing from 2018, full telemetry from 2018 onward.
- **Why we want it:** it already provides a **`Distance` channel** and lap alignment
  helpers — exactly the "align both laps by distance" step in the project doc.
  `lap.get_car_data().add_distance()`, `session.laps.pick_driver('VER').pick_fastest()`,
  interpolated/merged telemetry, corner/circuit info via `session.get_circuit_info()`.
- **Tradeoff:** heavier (loads whole sessions), needs a cache dir, slower cold start.
  Great for the ML training pipeline and deep single-lap analysis; less ideal for
  quick live polling.

**Our split:** OpenF1 for live/quick queries and the session picker; FastF1 for the
distance-aligned telemetry traces, corner detection, and building the ML training set.

## 3. Jolpica-F1 — https://github.com/jolpica/jolpica-f1  (TERTIARY)
The community successor to the **Ergast API**, which shut down after the 2024 season.
Ergast-compatible schema. Use for schedules, race results, qualifying results, and
championship standings back to 1950. FastF1 already speaks this API, so we mostly get
it "for free" through FastF1.

## Others considered
- **Official F1 Live Timing (SignalR)** — what FastF1 wraps; don't hit directly.
- **Ergast** — deprecated/frozen; use Jolpica instead.
- **f1nsight / community datasets** — reference only.

## Practical rules for the team
1. Respect OpenF1 rate limits — **cache aggressively** (P1). A session's telemetry
   doesn't change once the session is over; cache it hard.
2. Prefer **FastF1's cache** for training data (`ml/`) so reruns are cheap.
3. Keep data-source access behind `clients/` so we can swap sources without touching
   analysis/ML/frontend code.
4. Non-commercial only — this is a student/portfolio project; don't ship it as a paid product.
