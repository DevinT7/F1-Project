"""Phase 9 observability: structured logs, Prometheus metrics, Sentry. docs/monitoring.md is the guide.

  configure_logging()     structlog everywhere; LOG_FORMAT=json (the container default) renders one JSON
                          object per line, with the request id bound for the life of each request. Std-lib
                          loggers (uvicorn, sqlalchemy, rq) go through the same renderer. Keys that could
                          carry a secret or PII are dropped before rendering (REDACT_KEYS).
  init_sentry(component)  error reporting when SENTRY_DSN is set (no-op otherwise): no PII, no request
                          bodies or cookies, the request id as a tag.
  HTTP / CACHE / LLM_*    prometheus_client metrics recorded in the API process. With several uvicorn
                          workers set PROMETHEUS_MULTIPROC_DIR (the image does) so /metrics sums them.
  job_metrics()           job durations / outcomes and model drift, written by whichever process runs the
                          job (the RQ worker in production) to Redis, and read by the API at scrape time,
                          together with the queue depth: the worker itself is never scraped.

Metric names are listed in docs/monitoring.md; the dashboard (monitoring/grafana/raceiq.json) and the alert
rules (monitoring/prometheus/alerts.yml) use exactly these.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from typing import Any

import structlog
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily
from prometheus_client.registry import Collector

NS = "raceiq"
REDACT_KEYS = frozenset({"authorization", "cookie", "password", "token", "access_token", "refresh_token",
                         "api_key", "secret", "email", "factsheet", "question"})

# ---------------------------------------------------------------- logging
_configured = False


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in [k for k in event if k.lower() in REDACT_KEYS]:
        event[key] = "[redacted]"
    return event


def configure_logging(*, fmt: str | None = None, level: str | None = None, force: bool = False) -> None:
    """Idempotent. fmt: "json" | "console" (default: LOG_FORMAT, else console)."""
    global _configured
    if _configured and not force:
        return
    fmt = (fmt or os.environ.get("LOG_FORMAT") or "console").lower()
    lvl = getattr(logging, (level or os.environ.get("LOG_LEVEL") or "INFO").upper(), logging.INFO)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
    ]
    renderer: Any = (structlog.processors.JSONRenderer(sort_keys=False) if fmt == "json"
                     else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,         # structlog.testing.capture_logs keeps working
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info, renderer]))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True     # replaced by http.request (no client IPs)
    _configured = True


def bind_request(request_id: str) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)


def current_request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return str(value) if value else None


# ---------------------------------------------------------------- Sentry
def _sentry_before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("cookies", None)
        request.pop("data", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in ("authorization", "cookie", "x-admin-token"):
                    headers[key] = "[redacted]"
    event.pop("user", None)
    rid = current_request_id()
    if rid:
        event.setdefault("tags", {})["request_id"] = rid
    return event


def init_sentry(component: str) -> bool:
    """Report errors to Sentry when SENTRY_DSN is set. Returns whether it was enabled."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    import sentry_sdk
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("APP_ENV", "development"),
        release=os.environ.get("APP_VERSION") or None,
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        send_default_pii=False,
        max_request_body_size="never",
        before_send=_sentry_before_send,  # type: ignore[arg-type]
    )
    sentry_sdk.set_tag("component", component)
    return True


def sentry_message(message: str, *, level: str = "warning", fingerprint: list[str] | None = None,
                   tags: Mapping[str, str] | None = None) -> None:
    """A grouped Sentry event (ingest failures, LLM fallbacks): its frequency is what the alerts watch."""
    try:
        import sentry_sdk
        if not sentry_sdk.get_client().is_active():
            return
        with sentry_sdk.new_scope() as scope:
            if fingerprint:
                scope.fingerprint = fingerprint
            for k, v in (tags or {}).items():
                scope.set_tag(k, v)
            scope.capture_message(message, level=level)  # type: ignore[arg-type]
    except Exception:                                        # noqa: BLE001 — telemetry never breaks a request
        pass


# ---------------------------------------------------------------- API-process metrics
LATENCY_BUCKETS_S = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
LLM_BUCKETS_S = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
JOB_BUCKETS_S = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 3600.0, 10800.0)

HTTP_REQUESTS = Counter(f"{NS}_http_requests_total", "HTTP requests by route template and status.",
                        ["method", "route", "status"])
HTTP_LATENCY = Histogram(f"{NS}_http_request_duration_seconds", "HTTP request latency by route template.",
                         ["method", "route"], buckets=LATENCY_BUCKETS_S)
CACHE_LOOKUPS = Counter(f"{NS}_cache_lookups_total", "Response-cache lookups (comparison, fact sheet, report, …).",
                        ["kind", "result"])
ENGINEER_REQUESTS = Counter(f"{NS}_engineer_requests_total",
                            "Engineer reports / answers by outcome (llm_first_try, llm_after_retry, template_*, …).",
                            ["kind", "outcome"])
LLM_LATENCY = Histogram(f"{NS}_llm_latency_seconds", "Latency of each LLM attempt.", buckets=LLM_BUCKETS_S)
LLM_TOKENS = Counter(f"{NS}_llm_tokens_total", "LLM tokens by direction (in / out).", ["direction"])
LLM_COST = Counter(f"{NS}_llm_cost_usd_total", "Estimated LLM cost (LLM_PRICE_IN/OUT_PER_MTOK).")
LLM_VIOLATIONS = Counter(f"{NS}_llm_violations_total", "First-attempt validator violations by rule.", ["rule"])
LLM_FALLBACK_RULES = Counter(f"{NS}_llm_fallback_rules_total",
                             "Validator rules that forced a template (both attempts failed).", ["rule"])


# Pre-create the engineer series that alerts divide by, at 0 (see SharedStateCollector.collect).
for _kind in ("report", "ask"):
    for _outcome in ("llm_first_try", "llm_after_retry", "template_validation", "template_provider_error"):
        ENGINEER_REQUESTS.labels(_kind, _outcome)


def http_route(scope: Mapping[str, Any]) -> str:
    """The matched route template ("/api/v1/sessions/{session_key}"), never the raw path: bounded labels."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if not template:
        return "unmatched"
    # Nested routers (FastAPI ≥ 0.13x) keep the route's own template without the parent's prefix
    # ("/sessions/{session_key}" under "/api/v1"). Every prefix here is static, so it is whatever of the
    # request path precedes the route's own (substituted) part.
    concrete = str(template)
    for name, value in (scope.get("path_params") or {}).items():
        concrete = concrete.replace("{" + name + "}", str(value))
    path = str(scope.get("path", ""))
    if path.endswith(concrete) and concrete:
        return path[: len(path) - len(concrete)] + str(template)
    return str(template)


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


# ---------------------------------------------------------------- job metrics + drift (shared via Redis)
JOB_KINDS = ("ingest", "train")
JOB_OUTCOMES = ("succeeded", "failed", "retrying")
JOBS_KEY = f"{NS}:metrics:jobs"
DRIFT_KEY = f"{NS}:metrics:drift"
DRIFT_WINDOW = 10                      # sessions in the rolling MAE


class JobMetrics:
    """Job outcome counts, duration histograms, and the model-drift window. In memory when there is no
    Redis (dev / tests: the API process runs the jobs itself); in Redis otherwise."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self.redis = redis_client
        self._lock = threading.Lock()
        self._hash: dict[str, float] = {}
        self._drift: list[str] = []

    # writes
    def _incr(self, fields: Mapping[str, float]) -> None:
        if self.redis is not None:
            pipe = self.redis.pipeline()
            for f, v in fields.items():
                pipe.hincrbyfloat(JOBS_KEY, f, v)
            pipe.execute()
            return
        with self._lock:
            for f, v in fields.items():
                self._hash[f] = self._hash.get(f, 0.0) + v

    def record_job(self, kind: str, outcome: str, seconds: float) -> None:
        fields: dict[str, float] = {f"n|{kind}|{outcome}": 1, f"sum|{kind}": seconds, f"count|{kind}": 1}
        for b in JOB_BUCKETS_S:
            if seconds <= b:
                fields[f"le|{kind}|{b}"] = 1
        self._incr(fields)

    def record_drift(self, session_key: str, mae: float, n: int) -> None:
        item = json.dumps({"session": session_key, "mae": round(mae, 5), "n": n, "at": round(time.time())})
        if self.redis is not None:
            pipe = self.redis.pipeline()
            pipe.lpush(DRIFT_KEY, item)
            pipe.ltrim(DRIFT_KEY, 0, DRIFT_WINDOW - 1)
            pipe.execute()
            return
        with self._lock:
            self._drift = [item, *self._drift][:DRIFT_WINDOW]

    # reads
    def job_fields(self) -> dict[str, float]:
        if self.redis is not None:
            return {(k.decode() if isinstance(k, bytes) else k): float(v)
                    for k, v in self.redis.hgetall(JOBS_KEY).items()}
        with self._lock:
            return dict(self._hash)

    def drift_window(self) -> list[dict[str, Any]]:
        raw = self.redis.lrange(DRIFT_KEY, 0, DRIFT_WINDOW - 1) if self.redis is not None else list(self._drift)
        return [json.loads(r) for r in raw]

    def queue_depth(self) -> dict[str, int]:
        """RQ queued / scheduled (delayed retries) / started / failed. Empty without Redis."""
        if self.redis is None:
            return {}
        from .jobs.queues import QUEUE_NAME
        q = f"rq:queue:{QUEUE_NAME}"
        return {"queued": int(self.redis.llen(q)),
                "scheduled": int(self.redis.zcard(f"rq:scheduled:{QUEUE_NAME}")),
                "started": int(self.redis.zcard(f"rq:wip:{QUEUE_NAME}")),
                "failed": int(self.redis.zcard(f"rq:failed:{QUEUE_NAME}"))}

    def reset(self) -> None:
        if self.redis is not None:
            self.redis.delete(JOBS_KEY, DRIFT_KEY)
        with self._lock:
            self._hash.clear()
            self._drift.clear()


_job_metrics: JobMetrics | None = None
_job_metrics_lock = threading.Lock()


def job_metrics() -> JobMetrics:
    global _job_metrics
    with _job_metrics_lock:
        if _job_metrics is None:
            from .config import settings
            client = None
            if settings.redis_url:
                import redis
                client = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
            _job_metrics = JobMetrics(client)
        return _job_metrics


def set_job_metrics(metrics: JobMetrics | None) -> None:
    """Tests: swap the sink (None → rebuild from settings on next use)."""
    global _job_metrics
    with _job_metrics_lock:
        _job_metrics = metrics


def rolling_mae(window: Iterable[Mapping[str, Any]]) -> float | None:
    """Forecast-step-weighted MAE over the window (a 3-stint session counts less than a 40-stint one)."""
    items = [(float(w["mae"]), int(w["n"])) for w in window if int(w.get("n", 0)) > 0]
    total = sum(n for _, n in items)
    return sum(m * n for m, n in items) / total if total else None


class SharedStateCollector(Collector):
    """Read at scrape time: queue depth, job metrics and drift from the shared sink, plus the production
    model's recorded baseline MAE. Failing reads produce no samples (and a log line), never a 500."""

    def __init__(self, baseline_mae: Any = None) -> None:
        self.baseline_mae = baseline_mae          # () -> float | None

    def collect(self) -> Iterator[Any]:
        sink = job_metrics()
        try:
            depth = sink.queue_depth()
            fields = sink.job_fields()
            window = sink.drift_window()
        except Exception as exc:                     # noqa: BLE001
            structlog.get_logger("raceiq.metrics").warning("metrics.shared_state_unavailable", error=type(exc).__name__)
            return
        g = GaugeMetricFamily(f"{NS}_job_queue_depth", "Jobs in the RQ queue by state.", labels=["state"])
        for state, n in depth.items():
            g.add_metric([state], n)
        yield g
        c = CounterMetricFamily(f"{NS}_jobs", "Job attempts by kind and outcome (succeeded / failed / retrying).",
                                labels=["kind", "outcome"])
        # every known series exists from the first scrape at 0: a counter that is born at 1 shows no
        # increase(), so the first ingest failure would never alert
        counts = {(k, o): 0.0 for k in JOB_KINDS for o in JOB_OUTCOMES}
        kinds: set[str] = set(JOB_KINDS)
        for f, v in fields.items():
            parts = f.split("|")
            if parts[0] == "n":
                counts[(parts[1], parts[2])] = v
            if parts[0] == "count":
                kinds.add(parts[1])
        for (kind, outcome), v in sorted(counts.items()):
            c.add_metric([kind, outcome], v)
        yield c
        h = HistogramMetricFamily(f"{NS}_job_duration_seconds", "Job attempt duration (ingest, train).",
                                  labels=["kind"])
        for kind in sorted(kinds):
            buckets = [(str(b), fields.get(f"le|{kind}|{b}", 0.0)) for b in JOB_BUCKETS_S]
            buckets.append(("+Inf", fields.get(f"count|{kind}", 0.0)))
            h.add_metric([kind], buckets, sum_value=fields.get(f"sum|{kind}", 0.0))
        yield h
        mae = rolling_mae(window)
        d = GaugeMetricFamily(f"{NS}_tyre_model_rolling_mae_seconds",
                              f"Production tyre model h5 MAE on the last {DRIFT_WINDOW} newly ingested races.")
        if mae is not None:
            d.add_metric([], mae)
        yield d
        yield GaugeMetricFamily(f"{NS}_tyre_model_drift_sessions", "Races in the drift window.", value=len(window))
        base = self.baseline_mae() if self.baseline_mae else None
        b = GaugeMetricFamily(f"{NS}_tyre_model_baseline_mae_seconds",
                              "The production model's walk-forward h5 MAE recorded at training time.")
        if base is not None:
            b.add_metric([], float(base))
        yield b


def _production_baseline_mae() -> float | None:
    try:
        from .api.deps import get_model_registry
        return float(get_model_registry().production().metrics["walk_forward_h5_mae"])
    except Exception:                                # noqa: BLE001
        return None


_shared_collector = SharedStateCollector(_production_baseline_mae)


def render_metrics() -> tuple[bytes, str]:
    """The /metrics payload: the API process metrics (summed over workers in multiprocess mode) plus the
    shared state."""
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
    shared = CollectorRegistry()
    shared.register(_shared_collector)
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess
        local = CollectorRegistry()
        multiprocess.MultiProcessCollector(local)  # type: ignore[no-untyped-call]
        return generate_latest(local) + generate_latest(shared), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY) + generate_latest(shared), CONTENT_TYPE_LATEST
