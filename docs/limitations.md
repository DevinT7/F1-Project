# Known limitations and data policies (AI race engineer)

## User questions: retention policy (Phase 7F)

| What | Where | How long |
|---|---|---|
| Question text and the returned (validated) answer | the conversation store (`backend/app/ai/qa/conversation.py`), keyed by a random `conversation_id`: Redis when `RACEIQ_REDIS_URL` is set (shared by all workers), else process memory | the last **6 turns** per conversation; expires after **6 hours** idle (Redis TTL / memory sweep); in memory at most 1000 conversations, lost on restart |
| Question text in logs | **not logged** (only intent, outcome and hashes); logged only with the debug flag `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1`, which must not be set in production | — |
| Question text sent to the LLM provider | only when an LLM is configured, inside the user message (never in the system prompt); the provider's own retention policy applies | provider-defined |
| Failed LLM drafts | never stored or shown; used only inside that request's single retry | — |

- **Not persisted.** Questions are never written to the database or to disk (Redis keeps them only until the TTL), and they are not used for training or analytics. They are not linked to accounts.
- **Policy changes** (turn limit, TTL) are recorded here first.

## Accounts (Phase 8)

- **Stored data.** Email (lower-cased), an argon2id password hash, role and creation time; saved comparisons are parameters only.
- **Without a database** (`RACEIQ_DATABASE_URL` unset), accounts are in memory and vanish on restart: local development only.
- **No email verification or password reset yet.** Admins are designated by `RACEIQ_ADMIN_EMAILS`.

## What the engineer cannot know

- **Traffic, wind, setup, fuel, damage:** not in the data. Answers say traffic cannot be assessed.
- **Causes are rule-based contributing factors**, not proof (`docs/validator_rules.md`).
- **Tyre forecasts:** races only, with ≥ 5 clean laps in the stint. The pit window is a heuristic that ignores undercut/overcut, traffic, track position, safety-car probability and tyre availability.
- **Qualifying "tyre" questions** get "unavailable".

## Operational

- **Shared state.** With Redis configured, the report cache, the FactSheet store (`GET /engineer/facts/{hash}`, 24 h), conversations, rate limits and jobs are shared by every worker. Without Redis they are per process. The engineer metrics (`app/ai/metrics.py`) are always per process.
- **Rate limits** are enforced since Phase 8 (`app/ratelimit.py`, docs/security.md).
- **The live LLM evaluation** (7D, 7E, 7F) is pending an API key (`docs/api_keys.md`).
