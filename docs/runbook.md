# Runbook: AI race engineer (Phase 7F)

Endpoints: `POST /api/v1/engineer/report`, `POST /api/v1/engineer/ask`, `GET /api/v1/engineer/facts/{factsheet_hash}`.
Code: `backend/app/api/engineer_service.py`, `backend/app/api/routes/engineer.py`, `backend/app/ai/`.

## Switches

| Setting | Effect |
|---|---|
| `ENGINEER_LLM_ENABLED=false` (or `RACEIQ_ENGINEER_LLM_ENABLED`) | **Kill switch.** Every report and answer is the validated template (`outcome: template_disabled`); the LLM client is never built or called. Takes effect on restart. |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` | The LLM (`docs/prompts.md`). Unset → templates (`template_no_llm`), one `ai.llm_unconfigured` warning. |
| `RACEIQ_ADMIN_TOKEN` | Enables `GET /api/v1/engineer/metrics` (header `X-Admin-Token`). Unset → the endpoint returns 404. |
| `RACEIQ_ENGINEER_FAKE_LLM=inject` | **Test only** (Playwright safety test). A scripted LLM that injects an ungrounded number and a fabricated cause; logs a warning at startup. Never set in production. |
| `RACEIQ_AI_DEBUG_LOG_FACTSHEET=1` | Debug only: logs prompts (FactSheet slices) and question text. Never in production. |
| `NEXT_PUBLIC_FEATURE_ENGINEER=0` | Hides the engineer card in the frontend (build-time). |

## Metrics

Read them with:

```
curl -s -H "X-Admin-Token: $RACEIQ_ADMIN_TOKEN" http://localhost:8000/api/v1/engineer/metrics | jq
```

The counters are per process and reset on restart (`backend/app/ai/metrics.py`; Phase 8 exports the same names to Prometheus).

| Key | Meaning |
|---|---|
| `requests_total` | per `report:` / `ask:` and outcome: `llm_first_try`, `llm_after_retry`, `template_validation`, `template_provider_error`, `template_no_llm`, `template_disabled`, `refused`, `clarification` |
| `rates.report_first_try`, `report_after_retry`, `report_fallback` | share of report requests by outcome (cache hits included) |
| `rates.ask_valid_within_retry`, `ask_fallback`, `out_of_scope` | Q&A outcome shares; `out_of_scope` = refused-intent share of all questions |
| `rates.cache_hit_ratio` | report cache hits / lookups |
| `violations_total{rule}` | first-attempt validator violations (what the model gets wrong) |
| `fallback_rules_total{rule}` | rules that forced a template after the retry |
| `llm_latency_ms` | per LLM attempt (count, mean, p50, p95, buckets) |
| `request_latency_ms{kind:path}` | per request; path `llm`, `template` or `cache` |
| `tokens_total`, `cost_usd_total` | cost needs `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` |

**Alert on:**

- `rates.report_fallback` > 0.2 sustained: the prompt or model regressed. Check `fallback_rules_total`.
- `llm_latency_ms.p95` > 8000.
- Any `template_provider_error` burst: the provider is down. Consider the kill switch.

## Logs

There is one structured line per request: `ai.engineer_request`, with `kind`, `factsheet_hash`, `prompt_version`, `outcome`, `cached`, `latency_ms`, `request_id` and `fallback_reason` (`intent` for ask). It never carries the FactSheet or the question text unless the debug flag is set.

**Per LLM attempt:** `ai.report_attempt` / `ai.qa_attempt`, with model, tokens, latency and violated rules. Provider calls log `ai.llm_call` / `ai.llm_call_failed`, with status, request id and transport attempt. API keys never appear.

**Log queries** (JSON logs, e.g. `jq`):

```
# fallback reasons in the last run
jq -r 'select(.event=="ai.engineer_request" and .outcome=="template_validation") | .fallback_reason' app.log | sort | uniq -c
# one user-reported problem: the X-Request-ID from the UI's error chip
jq 'select(.request_id=="<id>")' app.log
# what a FactSheet said (facts are kept in memory, newest 128)
curl -s http://localhost:8000/api/v1/engineer/facts/<factsheet_hash> | jq '.facts[] | select(.id|startswith("T6."))'
```

## Incidents

| Symptom | Action |
|---|---|
| AI text looks wrong | The text passed the validator. Fetch the facts by `factsheet_hash` and file the bullet as a validator corpus case (`backend/tests/ai/corpus/bad.jsonl`). Bump the prompt version if the prompt is changed. |
| Provider outage, slow responses | Set `ENGINEER_LLM_ENABLED=false` and restart. Users get template summaries badged "Template summary". |
| Cost spike | Check `tokens_total` and `cache_hit_ratio`; the kill switch stops all spend. Rate limits are a Phase 8 item (`deps.engineer_rate_limit` is a stub). |
