# Known limitations and data policies

## Data: what RaceIQ cannot know

RaceIQ analyses what FastF1 publishes from F1's timing feed: car telemetry, lap and sector times, tyre stints, weather and race control. The following are not in that data, so no feature, finding or AI sentence may claim them:

- **No brake pressure.** `Brake` is an on/off flag (docs/decisions.md, Phase 1).
  - **What RaceIQ can say:** where braking starts and how long it lasts, read from the flag and the speed trace.
  - **What it cannot say:** how hard a driver brakes, brake balance, or trail-braking technique.
- **No true traffic gaps.** There is no reliable gap-to-car-ahead data for every lap.
  - Every tyre dataset row carries `traffic_known = False`, and no traffic feature exists.
  - A lap lost to traffic looks like a slow lap.
  - The engineer says traffic cannot be assessed, rather than guessing.
- **Pit windows are heuristic.**
  - **How it works:** the window is where the forecast pace loss crosses a threshold (`interval_crossing_v2`, docs/decisions.md 6E).
  - **What it ignores:** undercut / overcut, traffic, track position, safety-car probability, tyre allocation and team strategy.
  - **How to read it:** an indication of when the current tyres stop paying, not a strategy call. It is also unavailable for about 39% of real stints, where the interval is too wide.
- **Telemetry is sampled and interpolated.** About 700–1000 samples per lap, the first one interpolated. Corner minimum speeds and braking points are accurate to a few metres, not to the centimetre. Both laps are resampled onto a common distance grid before comparing.
- **Sessions RaceIQ has stored only.** Historical sessions, ingested on demand; no live timing. Since 2026-10-15, F1's live-timing server rejects anonymous requests (HTTP 401), so new ingests fail until FastF1 is upgraded (3.8.3 is out) and re-verified. Stored sessions are unaffected (docs/runbook.md → Re-ingest a session).
- **Unofficial data.** FastF1, OpenF1 and Jolpica are community projects, not F1 products. Timing can be corrected after the fact, and occasional laps or channels are missing; the ingest validates and marks those laps rather than filling them in.

## Not affiliated with Formula 1

RaceIQ is an unofficial, non-commercial portfolio project. It is not associated in any way with the Formula 1 companies. F1, FORMULA ONE, FORMULA 1, FIA FORMULA ONE WORLD CHAMPIONSHIP, GRAND PRIX and related marks are trade marks of Formula One Licensing B.V. Team and driver names are used only to identify the data. Timing data is retrieved through the open-source [FastF1](https://github.com/theOehrly/Fast-F1) library, subject to its terms and to the rights of the data's owners.

# AI race engineer: data policies and limits

## User questions: retention policy (Phase 7F)

| What | Where | How long |
|---|---|---|
| Question text and the returned (validated) answer | the conversation store (`backend/app/ai/qa/conversation.py`), keyed by a random `conversation_id`: Redis when `RACEIQ_REDIS_URL` is set (shared by all workers), else process memory | the last **6 turns** per conversation; expires after **6 hours** idle (Redis TTL / memory sweep); in memory at most 1000 conversations, lost on restart |
| Question text in logs | **not logged** (only intent, outcome and hashes); logged only with the debug flag `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1`, which must not be set in production | — |
| Question text sent to the LLM provider | only when an LLM is configured, inside the user message (never in the system prompt); the provider's own retention policy applies | provider-defined |
| Failed LLM drafts | never stored or shown; used only inside that request's single retry | — |

- **Not persisted.** Questions are never written to the database or to disk (Redis keeps them only until the TTL), and they are not used for training or analytics. They are not linked to accounts.
- **Policy changes** (turn limit, TTL) are recorded here first.

## Accounts (Phase 8; operators only since 2026-10-15)

- **The web app has no sign-in.** Nobody needs an account to use any feature, the AI engineer included. The backend's accounts remain for operators: an admin token (POST /api/v1/auth/login with an address in `RACEIQ_ADMIN_EMAILS`) is what ingest, training and the engineer metrics require.

- **Stored data.** Email (lower-cased), an argon2id password hash, role and creation time; saved comparisons are parameters only.
- **Without a database** (`RACEIQ_DATABASE_URL` unset), accounts are in memory and vanish on restart: local development only.
- **No email verification or password reset yet.** Admins are designated by `RACEIQ_ADMIN_EMAILS`.

## What the engineer cannot know

- **Traffic, wind, setup, fuel, damage:** not in the data. Answers say traffic cannot be assessed.
- **Causes are rule-based contributing factors**, not proof (`docs/validator_rules.md`).
- **Tyre forecasts:** races only, with ≥ 5 clean laps in the stint. The pit window is a heuristic that ignores undercut/overcut, traffic, track position, safety-car probability and tyre availability.
- **Qualifying "tyre" questions** get "unavailable".

## Operational

- **Shared state.** With Redis configured, the report cache, the FactSheet store (`GET /engineer/facts/{hash}`, 24 h), conversations, rate limits and jobs are shared by every worker. Without Redis they are per process. The engineer metrics endpoint (`app/ai/metrics.py`) is per process; the Prometheus metrics (docs/monitoring.md) are summed over all workers.
- **Rate limits** are enforced since Phase 8 (`app/ratelimit.py`, docs/security.md).
- **The live LLM evaluation** (7D, 7E, 7F) is pending an API key (`docs/api_keys.md`).
