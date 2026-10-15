"""Phase 7F observability: in-process counters and histograms for the engineer endpoints.

    METRICS.record_report(...) / METRICS.record_answer(...) / METRICS.snapshot()

  requests_total{kind, outcome}           kind = report | ask; outcome = llm_first_try | llm_after_retry |
                                          template_validation | template_provider_error | template_no_llm |
                                          template_disabled | refused | clarification
  violations_total{rule}                  first-attempt validator violations (what the model gets wrong)
  fallback_rules_total{rule}              rules that forced a template (both attempts failed)
  llm_latency_ms                          histogram, per LLM attempt
  request_latency_ms{kind, path}          histogram, path = llm | template | cache
  tokens_total{direction}                 in / out
  cost_usd_total                          estimated from LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK
  cache_total{kind, result}               hit / miss
  intents_total{intent}                   Q&A routing distribution
Derived in the snapshot: cache hit ratio, out-of-scope rate, first-try / after-retry / fallback rates.

Per process and reset on restart. Read them via GET /api/v1/engineer/metrics (admin token) —
docs/runbook.md. Phase 9 mirrors them to Prometheus (raceiq_engineer_requests_total, raceiq_llm_*;
app/observability.py), which sums over workers and survives restarts in the time series.
"""
from __future__ import annotations

import os
import threading
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .. import observability as obs
from .guarded import Attempt
from .validator import Violation

BUCKETS_MS = (50, 100, 200, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000)
LLM_OUTCOMES = ("llm_first_try", "llm_after_retry")


@dataclass
class Histogram:
    buckets: tuple[int, ...] = BUCKETS_MS
    counts: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))
    total: float = 0.0
    n: int = 0
    samples: list[float] = field(default_factory=list)       # the last 1000, for percentiles

    def observe(self, value: float) -> None:
        self.counts[bisect_left(self.buckets, value)] += 1
        self.total += value
        self.n += 1
        self.samples.append(value)
        if len(self.samples) > 1000:
            del self.samples[:-1000]

    def percentile(self, q: float) -> float | None:
        if not self.samples:
            return None
        s = sorted(self.samples)
        return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]

    def as_dict(self) -> dict[str, Any]:
        labels = [f"le_{b}" for b in self.buckets] + ["le_inf"]
        return {"count": self.n, "mean": round(self.total / self.n, 1) if self.n else None,
                "p50": self.percentile(0.5), "p95": self.percentile(0.95),
                "buckets": dict(zip(labels, self.counts, strict=True))}


def _price(name: str) -> float | None:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.requests: Counter[tuple[str, str]] = Counter()
        self.violations: Counter[str] = Counter()
        self.fallback_rules: Counter[str] = Counter()
        self.tokens: Counter[str] = Counter()
        self.cache: Counter[tuple[str, str]] = Counter()
        self.intents: Counter[str] = Counter()
        self.cost_usd = 0.0
        self.llm_latency = Histogram()
        self.request_latency: dict[tuple[str, str], Histogram] = {}

    # ------------------------------------------------------------ recording
    def _attempts(self, attempts: Iterable[Attempt]) -> None:
        pin, pout = _price("LLM_PRICE_IN_PER_MTOK"), _price("LLM_PRICE_OUT_PER_MTOK")
        for a in attempts:
            if a.model is not None:
                self.llm_latency.observe(a.latency_ms)
                obs.LLM_LATENCY.observe(a.latency_ms / 1000)
            self.tokens["in"] += a.tokens_in
            self.tokens["out"] += a.tokens_out
            obs.LLM_TOKENS.labels("in").inc(a.tokens_in)
            obs.LLM_TOKENS.labels("out").inc(a.tokens_out)
            if pin is not None and pout is not None:
                cost = (a.tokens_in * pin + a.tokens_out * pout) / 1e6
                self.cost_usd += cost
                obs.LLM_COST.inc(cost)

    def _record(self, kind: str, outcome: str, *, cached: bool, latency_ms: float, attempts: Iterable[Attempt],
                first: Iterable[Violation], fallback_reason: str | None) -> None:
        obs.ENGINEER_REQUESTS.labels(kind, outcome).inc()
        if not cached:
            for v in (first := list(first)):
                obs.LLM_VIOLATIONS.labels(v.rule).inc()
            if outcome == "template_validation" and fallback_reason:
                for r in (r for r in fallback_reason.split(",") if r):
                    obs.LLM_FALLBACK_RULES.labels(r).inc()
            if outcome in ("template_validation", "template_provider_error"):
                obs.sentry_message(f"engineer {kind} fell back to the template", fingerprint=["llm-fallback", kind],
                                   tags={"engineer_kind": kind, "outcome": outcome})
        with self._lock:
            self.requests[(kind, outcome)] += 1
            self.cache[(kind, "hit" if cached else "miss")] += 1
            path = "cache" if cached else ("llm" if outcome.startswith("llm") or outcome == "template_validation"
                                           or outcome == "template_provider_error" else "template")
            self.request_latency.setdefault((kind, path), Histogram()).observe(latency_ms)
            if cached:
                return
            self._attempts(attempts)
            self.violations.update(v.rule for v in first)
            if outcome == "template_validation" and fallback_reason:
                self.fallback_rules.update(r for r in fallback_reason.split(",") if r)

    def record_report(self, outcome: str, *, cached: bool, latency_ms: float, attempts: Iterable[Attempt] = (),
                      first: Iterable[Violation] = (), fallback_reason: str | None = None) -> None:
        self._record("report", outcome, cached=cached, latency_ms=latency_ms, attempts=attempts, first=first,
                     fallback_reason=fallback_reason)

    def record_answer(self, outcome: str, intent: str, *, latency_ms: float, attempts: Iterable[Attempt] = (),
                      first: Iterable[Violation] = (), fallback_reason: str | None = None) -> None:
        with self._lock:
            self.intents[intent] += 1
        self._record("ask", outcome, cached=False, latency_ms=latency_ms, attempts=attempts, first=first,
                     fallback_reason=fallback_reason)

    # ------------------------------------------------------------ reading
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            def rate(kind: str, outcomes: tuple[str, ...]) -> float | None:
                total = sum(v for (k, _), v in self.requests.items() if k == kind)
                return round(sum(self.requests[(kind, o)] for o in outcomes) / total, 4) if total else None

            hits = sum(v for (_, r), v in self.cache.items() if r == "hit")
            lookups = sum(self.cache.values())
            asked = sum(self.intents.values())
            return {
                "requests_total": {f"{k}:{o}": v for (k, o), v in sorted(self.requests.items())},
                "rates": {
                    "report_first_try": rate("report", ("llm_first_try",)),
                    "report_after_retry": rate("report", ("llm_after_retry",)),
                    "report_fallback": rate("report", ("template_validation", "template_provider_error")),
                    "ask_valid_within_retry": rate("ask", LLM_OUTCOMES),
                    "ask_fallback": rate("ask", ("template_validation", "template_provider_error")),
                    "out_of_scope": round(self.intents["out_of_scope"] / asked, 4) if asked else None,
                    "cache_hit_ratio": round(hits / lookups, 4) if lookups else None,
                },
                "violations_total": dict(self.violations.most_common()),
                "fallback_rules_total": dict(self.fallback_rules.most_common()),
                "intents_total": dict(self.intents.most_common()),
                "tokens_total": dict(self.tokens),
                "cost_usd_total": round(self.cost_usd, 6),
                "cache_total": {f"{k}:{r}": v for (k, r), v in sorted(self.cache.items())},
                "llm_latency_ms": self.llm_latency.as_dict(),
                "request_latency_ms": {f"{k}:{p}": h.as_dict() for (k, p), h in sorted(self.request_latency.items())},
            }


METRICS = Metrics()
