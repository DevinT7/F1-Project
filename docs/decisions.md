# Decisions log

## Phase 1 — Data ingestion and caching (P1, 2026-10-12)

Code: `backend/app/ingestion/` (adapter, models, normalize, service, CLI) and
`backend/app/storage/repository.py`. Run from `backend/`:

```bash
python -m app.ingestion ingest --year 2024 --round 5 --session Q [--force]
python -m app.ingestion season --year 2024 --sessions Q,R
python -m app.ingestion events --year 2024
python -m app.ingestion list
```

### Where the plan and the repo differ

| Plan says | What we did | Why |
|---|---|---|
| `raceiq/ingestion/…`, `raceiq/storage/…` | `backend/app/ingestion/…`, `backend/app/storage/…` | The repo already has the `backend/app` package; P2 and P3 put their work there too. |
| `python -m raceiq.ingestion …` | `python -m app.ingestion …` (run from `backend/`) | Same reason. |
| `make test` | `cd backend && pytest` (network tests are excluded by `pytest.ini`) | The repo has no Makefile. |
| `session.load(..., messages=False)` | **`messages=True`** | FastF1 3.5.3 only fills in `Laps.Deleted` from race-control messages (`core.py::_set_laps_deleted_from_rcm`). Without them every lap reads `Deleted=None`, so the "not Deleted" validity rule can't work. Loading messages adds well under 1 s. |
| Fixture: 2 drivers × 3 laps | **3 drivers × 3 laps** (VER, PER, ALO), 1.0 MB | The acceptance criterion needs fastest-lap matches for ≥ 3 drivers in that fixture. |
| "only module that imports fastf1" | Also routed the legacy chart client (`app/clients/fastf1_client.py`) through `ingestion.fastf1_client.import_fastf1()` | Keeps a single import site. `ml/build_dataset.py` (P3's standalone pipeline) still imports fastf1 directly and is outside the isolation test's scope. |

### FastF1 3.5.3 schemas, probed on 2024 R5 (Chinese GP) Q and R

- **`session.laps`** (31 columns). Everything the plan lists is present. Dtypes to watch:
  `LapNumber`, `Stint`, `TyreLife` and `Position` are **float64** (NaN-able), not int.
  `DriverNumber` is a **string**. `Deleted` is **object** (bool, or None without
  messages). `TrackStatus` is a concatenated code string such as `"12"` or `"1267"`.
  There are extras we don't keep: `SpeedI1/I2/FL/ST`, `IsPersonalBest`, `Sector*SessionTime`,
  `LapStartDate`, `FastF1Generated`. Quali: 279 laps, 114 with no LapTime (out/in laps).
  Race: 1032 laps, 5 deleted for track limits.
- **`lap.get_telemetry()`** (18 columns): all planned channels are present (`Speed, RPM,
  nGear, Throttle, Brake, DRS, X, Y, Z, Time, SessionTime, Distance`). `Brake` is
  **bool**. `Time` is lap-relative and starts at 0. The first row is
  `Source="interpolation"`. `Distance` was already monotonic in every lap we checked.
  Extras: `Date`, `DriverAhead`, `DistanceToDriverAhead`, `RelativeDistance`, `Status`,
  `Source`. About 700–1000 samples per lap.
- **`session.weather_data`**: `Time, AirTemp, Humidity, Pressure, Rainfall, TrackTemp,
  WindDirection, WindSpeed`. **`WindSpeed` is m/s** (`_api.py` docstring), so we store
  `wind_kph = WindSpeed × 3.6`. One sample per minute.
- **`get_circuit_info().corners`**: `X, Y, Number, Letter, Angle, Distance`. `Letter`
  is `""` when there's no letter, which we store as None. `rotation` is a float (237.0
  at Shanghai). The raw table is kept in `corners_raw.json`.
- **`session.results`**: `Q1/Q2/Q3` are timedeltas. `TeamColor` is hex with no `#`
  (we add one). `Position` and `GridPosition` are float.
- **`session.date`** is a **naive** Timestamp, but its value is UTC (07:00 for a 15:00
  local Shanghai start). We attach UTC without shifting it. Schedule
  `Session{n}DateUtc` columns are treated the same way.
- `session.total_laps` is None for qualifying and 56 for the race. It's stored as
  `n_laps_scheduled`.

### DRS normalization

The FastF1 3.5.3 docs (`fastf1/_api.py`, `car_data`) say only: *"DRS (int): 0-14 (Odd
DRS is Disabled, Even DRS is Enabled?)"*. Codes observed across all 20 drivers' car
data in 2024 R5 Q: `0, 1, 2, 3, 8, 9, 10, 11, 12, 14`. The commonly used decoding is
0/1 = off, 8 = eligible (detected, not yet open), 10/12/14 = flap open, and odd codes
= disabled. We store the raw `drs_code` and set
**`drs_open = drs_code ∈ {10, 12, 14}`** (`normalize.DRS_OPEN_CODES`). The plan's
"≥ 10" rule would wrongly count code 11 as open. It appeared in only 26 samples, but
FastF1's own odd = disabled note excludes it.

### Lap validity (`LapRecord.is_valid_for_comparison`, `invalid_reason`)

Rules are checked in this order, and the first failure becomes the reason:
`no_lap_time` → `not_accurate` → `deleted` → `no_telemetry` → `too_few_samples`
(fewer than 200) → `distance_out_of_range`. The last one means the lap's telemetry
distance is more than ±3 % from the session median, where the median is taken over
laps that have both a lap time and telemetry. `deleted=None` (unknown) does **not**
invalidate a lap. On 2024 R5 Q this leaves 92 of 279 laps valid, and
`get_fastest_valid_lap` matches the official best of Q1/Q2/Q3 within 1 ms for **all
20 drivers**.

### Storage

- Each session is written to `data/{year}_{round:02d}_{code}/` at the repo root
  (gitignored, `RACEIQ_DATA_DIR` overrides it). Telemetry goes in
  `telemetry/{DRV}_{lap:03d}.parquet` and is read lazily per lap. Laps, weather and
  corners are Parquet (zstd). Session, drivers, raw corners and the manifest are JSON.
- Every file is written atomically (temp file + `os.replace`). A session is staged in a
  sibling `.staging-*` directory and swapped in, and `manifest.json` is written last.
  A crash at any point leaves the previous copy intact, or leaves no session at all.
- `models.SCHEMA_VERSION` (currently 1) is stored in the manifest. A mismatch makes
  `has_session` return False, which triggers a re-ingest. Bump it whenever a stored
  model changes.
- The schedule is cached at `data/_schedule/{year}.json`. Past seasons are cached
  forever. The current and future seasons refresh after 24 h.

### Performance (measured on this machine)

- Cold FastF1 load: 29 s for Q and 45 s for R (downloads). A warm cache takes about 4 s.
- `Lap.get_telemetry()` takes about **0.5 s per lap** and dominates ingest time: full Q
  ≈ 2.5 min (279 laps, 14 MB stored); full R ≈ 7.5 min (1032 laps, 757 valid, 5 deleted, 40 MB).
- A no-op re-ingest takes about 1.0 s wall, interpreter startup included.
- Follow-up if season batches get too slow: merge `car_data` and `pos_data` once per
  driver and slice by lap start/end, instead of calling `get_telemetry()` per lap.

### Earliest supported year

Only 2024 has been verified so far. FastF1 has timing and telemetry from 2018. Re-test
older seasons before claiming support for them. Missing telemetry is already handled:
the lap is marked `no_telemetry` and nothing crashes.

## Phase 6A — Tire dataset (P1, 2026-10-12)

Code: `backend/app/tires/` (`config.py`, `exclusions.py`, `dataset.py`, `stints.py`,
`report.py`). The plan's `raceiq/tires/` maps to `backend/app/tires/`, following the same
convention as Phases 1–5. The CLI is `python -m app.tires.dataset build --seasons 2023 2024`.

### Track-status codes (verified in FastF1 3.5.3, `fastf1/_api.py::track_status_data`)

| Code | Meaning | Treated as |
|---|---|---|
| `1` | Track clear | green |
| `2` | Yellow flag (sectors unknown) | non-green |
| `3` | "??? Never seen so far, does not exist?" (from the FastF1 docs) | non-green |
| `4` | Safety car | non-green, neutralization |
| `5` | Red flag | non-green, neutralization, restart detection |
| `6` | Virtual safety car deployed | non-green, neutralization |
| `7` | VSC ending | non-green, neutralization |

A lap's `TrackStatus` concatenates every code seen during the lap (`"12"`, `"1267"`), so the
rule treats it as a **set of characters**. A lap is kept only if that set is exactly `{"1"}`,
which means `"11"` is green and `"12"` is not. A missing status is not green.

### Rule interpretations not spelled out in the plan

- **`lap_1_or_restart`:** the restart lap is decided per driver. It is the lap whose previous
  lap (same driver) carried code `5` while this lap does not. Red-flag laps themselves fall to
  `non_green_track_status`.
- **`inaccurate_or_deleted`:** `Deleted = None` (unknown) does not count as deleted.
- **`stint_outlier`:** a stint whose MAD is 0 is left alone. A zero spread gives no basis for
  judging outliers, and with a zero threshold anything slower than the median would be dropped.
- **`laps_since_neutralization`:** counts from the most recent earlier lap number on which
  **any** driver carried code 4, 5, 6 or 7. Before the first one, the sentinel is 999. The lap
  number is a proxy for the leader's timeline, so it is approximate for lapped cars.
- **`prev_lap_time_s`:** the previous **kept** lap in the same stint, which can skip over excluded
  laps. It is null at the start of a stint. It may only be used as a feature where it would
  really be available, which means forecasting.
- **Stints endpoint `clean_laps`:** counts laps passing the lap-level rules (1–7).
  `model_eligible` then applies the stint-level rule (≥ 5 clean laps). This matches what the
  dataset keeps without reporting `clean_laps: 0` for a stint that simply has 4 clean laps.
- **Traffic:** there is no reliable gap data, so `traffic_known = False` on every row, and no
  traffic feature is derived.
- **Units:** `fuel_proxy = race_laps_total − lap_number` stays in laps. No kg conversion and no
  time-per-kg constant exist anywhere. `race_laps_total` falls back to the highest lap number
  when FastF1 has no scheduled distance.
