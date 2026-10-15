"""Phase 9 observability: JSON logs with the request id, redaction, /metrics (route-template labels, token,
production default), job metrics and model drift through the shared sink (memory and Redis), Sentry
scrubbing."""
from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
import structlog

from app import observability as obs
from app.config import settings
from app.jobs.queues import InlineQueue
from app.jobs.runner import JobSystem, PermanentJobError
from app.jobs.store import MemoryJobStore
from app.main import app
from app.storage.repository import ParquetRepository
from app.tires import drift
from app.tires.registry import ModelRegistry
from tests import tires_synth as S

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def sink():
    s = obs.JobMetrics()
    obs.set_job_metrics(s)
    yield s
    obs.set_job_metrics(None)


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _sample(text: str, name: str, **labels: str) -> float | None:
    want = ",".join(f'{k}="{v}"' for k, v in labels.items())
    for line in text.splitlines():
        if line.startswith(f"{name}{{{want}}} ") or (not labels and line.startswith(f"{name} ")):
            return float(line.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------- logs
@pytest.fixture
def json_logs():
    stream = io.StringIO()
    obs.configure_logging(fmt="json", force=True)
    handler = logging.getLogger().handlers[0]
    handler.setStream(stream)  # type: ignore[attr-defined]
    yield stream
    obs.configure_logging(force=True)


async def test_every_request_logs_one_json_line_with_its_request_id(client, json_logs):
    r = await client.get("/api/v1/sessions/2024-05-Q", headers={"X-Request-ID": "trace-abc.123"})
    assert r.headers["x-request-id"] == "trace-abc.123"
    lines = [json.loads(line) for line in json_logs.getvalue().splitlines() if line.strip()]
    access = [entry for entry in lines if entry.get("event") == "http.request"]
    assert len(access) == 1
    entry = access[0]
    assert entry["request_id"] == "trace-abc.123" and entry["route"] == "/api/v1/sessions/{session_key}"
    assert entry["status"] == r.status_code and entry["level"] == "info" and "timestamp" in entry
    assert "client" not in entry and "path" not in entry                 # no IPs, no raw paths / query strings
    assert all(entry.get("request_id") == "trace-abc.123" for entry in lines)


def test_secret_and_pii_keys_are_redacted(json_logs):
    structlog.get_logger("t").info("x", email="a@b.c", token="t0k", Authorization="Bearer z", laps=3)
    entry = json.loads(json_logs.getvalue().splitlines()[-1])
    assert entry["email"] == entry["token"] == entry["Authorization"] == "[redacted]" and entry["laps"] == 3
    assert "a@b.c" not in json_logs.getvalue() and "t0k" not in json_logs.getvalue()


# ---------------------------------------------------------------- /metrics
async def test_metrics_use_route_templates_and_count_cache_lookups(client, sink, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", None)
    before = (await client.get("/metrics")).text
    n0 = _sample(before, "raceiq_http_requests_total", method="GET", route="/api/v1/sessions/{session_key}",
                 status="200") or 0
    await client.get("/api/v1/sessions/2031-01-Q")
    await client.get("/api/v1/sessions/2032-01-Q")
    await client.get("/definitely/not/a/route")
    text = (await client.get("/metrics")).text
    assert _sample(text, "raceiq_http_requests_total", method="GET", route="/api/v1/sessions/{session_key}",
                   status="200") == n0 + 2
    assert "2031-01-Q" not in text and "definitely" not in text           # bounded label values
    assert _sample(text, "raceiq_http_requests_total", method="GET", route="unmatched", status="404") is not None
    assert 'route="/metrics"' not in text                                 # scrapes are not counted
    assert "raceiq_http_request_duration_seconds_bucket" in text
    assert "raceiq_tyre_model_baseline_mae_seconds 0.3991" in text        # the production entry's walk-forward MAE


async def test_metrics_token_and_production_default(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "s3cret-metrics")
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer s3cret-metrics"})).status_code == 200
    monkeypatch.setattr(settings, "metrics_token", None)
    monkeypatch.setenv("APP_ENV", "production")
    assert (await client.get("/metrics")).status_code == 404


def test_cache_lookups_are_counted_by_kind():
    from app.caching import MemoryCache, cache_key, single_flight
    c = MemoryCache()
    hit0 = obs.CACHE_LOOKUPS.labels("cmp", "hit")._value.get()
    miss0 = obs.CACHE_LOOKUPS.labels("cmp", "miss")._value.get()
    key = cache_key("cmp", "2024_05_Q", "d1")
    single_flight(c, key, lambda: b"v", 60)
    single_flight(c, key, lambda: b"v", 60)
    assert obs.CACHE_LOOKUPS.labels("cmp", "miss")._value.get() == miss0 + 1
    assert obs.CACHE_LOOKUPS.labels("cmp", "hit")._value.get() == hit0 + 1


# ---------------------------------------------------------------- jobs
def _system(handlers) -> JobSystem:
    queue = InlineQueue()
    js = JobSystem(store=MemoryJobStore(), queue=queue, handlers=handlers, sleep=lambda s: None)
    queue.run = js.run_job
    return js


def test_job_outcomes_and_durations_reach_the_scrape(sink):
    def ok(job, progress):
        return {}

    def bad(job, progress):
        raise PermanentJobError("no such session")

    attempts = []

    def flaky(job, progress):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("network")
        return {}

    js = _system({"ingest": ok, "train": bad})
    js.submit("ingest", "2024_05_Q")
    js.submit("train", "all")
    js.handlers["ingest"] = flaky
    js.submit("ingest", "2024_06_R")
    fields = sink.job_fields()
    assert fields["n|ingest|succeeded"] == 2 and fields["n|ingest|retrying"] == 1
    assert fields["n|train|failed"] == 1 and fields["count|ingest"] == 3
    text = obs.render_metrics()[0].decode()
    assert _sample(text, "raceiq_jobs_total", kind="ingest", outcome="succeeded") == 2
    assert _sample(text, "raceiq_job_duration_seconds_bucket", kind="ingest", le="+Inf") == 3
    assert _sample(text, "raceiq_job_duration_seconds_bucket", kind="ingest", le="5.0") == 3


def test_job_series_exist_at_zero_before_the_first_job(sink):
    text = obs.render_metrics()[0].decode()
    for kind in obs.JOB_KINDS:
        for outcome in obs.JOB_OUTCOMES:
            assert _sample(text, "raceiq_jobs_total", kind=kind, outcome=outcome) == 0
        assert _sample(text, "raceiq_job_duration_seconds_count", kind=kind) == 0
    assert _sample(text, "raceiq_engineer_requests_total", kind="report", outcome="template_validation") is not None


def test_a_metrics_outage_never_fails_a_job():
    class Broken(obs.JobMetrics):
        def record_job(self, *a, **k):
            raise ConnectionError("redis down")

    obs.set_job_metrics(Broken())
    try:
        js = _system({"ingest": lambda job, progress: {"ok": True}})
        job = js.store.get(js.submit("ingest", "2024_05_Q").id)
        assert job is not None and job.status == "succeeded" and job.result == {"ok": True}
    finally:
        obs.set_job_metrics(None)


# ---------------------------------------------------------------- drift
@pytest.fixture(scope="module")
def production():
    reg = ModelRegistry(settings.tire_registry_path)
    return reg.load(reg.production())


def _long_race(key: str) -> list:
    """Two drivers, one 30-lap stint each (the evaluation needs ≥ 12 clean laps per stint)."""
    laps = []
    for driver in ("VER", "NOR"):
        for n in range(1, 31):
            wobble = 0.12 * ((n * 7 + len(driver)) % 5 - 2)
            laps.append(S.race_lap(key, driver, n, 1, n, 95.0 + 0.05 * n - 0.03 * n + wobble))
    return laps


class _Registry:
    def __init__(self, model, trained_through: str) -> None:
        self.model = model
        self.entry = SimpleNamespace(trained_through_session=trained_through, version="test")

    def production(self):
        return self.entry

    def load(self, entry):
        return self.model


def test_drift_scores_only_unseen_races_and_rolls(tmp_path, sink, production):
    repo = ParquetRepository(tmp_path / "data")
    for key in ("2023_01_R", "2024_02_R", "2024_03_R"):
        S.store_race(repo, key, laps=_long_race(key), total_laps=30)
    S.store_race(repo, "2024_04_Q", code="Q")
    reg = _Registry(production, trained_through="2023_01_R")
    assert drift.record_session(repo, "2023_01_R", reg, sink) == {"drift": "skipped", "reason": "in_training_data"}
    assert drift.record_session(repo, "2024_04_Q", reg, sink)["reason"] == "not_a_race"
    first = drift.record_session(repo, "2024_02_R", reg, sink)
    assert first["drift"] == "recorded" and first["mae_s"] > 0 and first["steps"] > 0
    drift.record_session(repo, "2024_03_R", reg, sink)
    window = sink.drift_window()
    assert [w["session"] for w in window] == ["2024_03_R", "2024_02_R"]
    text = obs.render_metrics()[0].decode()
    assert _sample(text, "raceiq_tyre_model_drift_sessions") == 2
    assert _sample(text, "raceiq_tyre_model_rolling_mae_seconds") == pytest.approx(obs.rolling_mae(window))


def test_rolling_mae_is_step_weighted_and_the_window_is_bounded(sink):
    assert obs.rolling_mae([]) is None
    assert obs.rolling_mae([{"mae": 1.0, "n": 1}, {"mae": 0.0, "n": 3}]) == pytest.approx(0.25)
    for i in range(obs.DRIFT_WINDOW + 5):
        sink.record_drift(f"2025_{i:02d}_R", 0.4, 10)
    assert len(sink.drift_window()) == obs.DRIFT_WINDOW
    assert sink.drift_window()[0]["session"] == f"2025_{obs.DRIFT_WINDOW + 4:02d}_R"   # newest first


def test_the_shared_sink_works_on_redis():
    from tests.test_cache import redis_url
    import redis
    client = redis.Redis.from_url(redis_url())
    client.flushdb()
    s = obs.JobMetrics(client)
    obs.set_job_metrics(s)
    try:
        s.record_job("ingest", "succeeded", 42.0)
        s.record_drift("2024_15_R", 0.5, 100)
        client.rpush("rq:queue:raceiq", "a", "b")
        text = obs.render_metrics()[0].decode()
        assert _sample(text, "raceiq_jobs_total", kind="ingest", outcome="succeeded") == 1
        assert _sample(text, "raceiq_job_duration_seconds_sum", kind="ingest") == 42.0
        assert _sample(text, "raceiq_job_duration_seconds_bucket", kind="ingest", le="30.0") == 0
        assert _sample(text, "raceiq_job_duration_seconds_bucket", kind="ingest", le="60.0") == 1
        assert _sample(text, "raceiq_job_queue_depth", state="queued") == 2
        assert _sample(text, "raceiq_tyre_model_rolling_mae_seconds") == 0.5
    finally:
        obs.set_job_metrics(None)
        client.flushdb()


# ---------------------------------------------------------------- Sentry
def test_sentry_events_are_scrubbed_and_tagged_with_the_request_id():
    obs.bind_request("rid-42")
    event = {"request": {"cookies": {"raceiq_refresh": "x"}, "data": {"password": "p"},
                         "headers": {"Authorization": "Bearer abc", "Accept": "*/*"}},
             "user": {"email": "a@b.c", "ip_address": "1.2.3.4"}}
    out = obs._sentry_before_send(event, {})
    assert out is not None
    assert "cookies" not in out["request"] and "data" not in out["request"] and "user" not in out
    assert out["request"]["headers"] == {"Authorization": "[redacted]", "Accept": "*/*"}
    assert out["tags"]["request_id"] == "rid-42"
    structlog.contextvars.clear_contextvars()


def test_sentry_is_off_without_a_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert obs.init_sentry("api") is False
    obs.sentry_message("nothing happens")                        # inactive client: a no-op, never raises
