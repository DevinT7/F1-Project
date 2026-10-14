# Known limitations and data policies (AI race engineer)

## User questions: retention policy (Phase 7F)

| What | Where | How long |
|---|---|---|
| Question text and the returned (validated) answer | in-process memory, the conversation store (`backend/app/ai/qa/conversation.py`), keyed by a random `conversation_id` | the last **6 turns** per conversation; a conversation expires after **6 hours** idle; at most 1000 conversations (oldest evicted); everything is lost on restart |
| Question text in logs | **not logged** (only intent, outcome and hashes); logged only with the debug flag `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1`, which must not be set in production | — |
| Question text sent to the LLM provider | only when an LLM is configured, inside the user message (never in the system prompt); the provider's own retention policy applies | provider-defined |
| Failed LLM drafts | never stored or shown; used only inside that request's single retry | — |

- **No persistence, no accounts.** Questions are not written to disk or a database, and they are not used for training or analytics.
- **Phase 8 move to Redis.** When the store moves to Redis, it must keep the same limits (6 turns, a TTL). Any change to this policy is recorded here first.

## What the engineer cannot know

- **Traffic, wind, setup, fuel, damage:** not in the data. Answers say traffic cannot be assessed.
- **Causes are rule-based contributing factors**, not proof (`docs/validator_rules.md`).
- **Tyre forecasts:** races only, with ≥ 5 clean laps in the stint. The pit window is a heuristic that ignores undercut/overcut, traffic, track position, safety-car probability and tyre availability.
- **Qualifying "tyre" questions** get "unavailable".

## Operational

- **Per-process state.** The report cache, the FactSheet store (newest 128, used for `GET /engineer/facts/{hash}`), conversations and metrics are per process and reset on restart. With several workers, a `conversation_id` only works on the worker that created it until Phase 8 (Redis).
- **Rate limits** are a stub (`deps.engineer_rate_limit`); Phase 8 enforces them.
- **The live LLM evaluation** (7D, 7E, 7F) is pending an API key (`docs/api_keys.md`).
