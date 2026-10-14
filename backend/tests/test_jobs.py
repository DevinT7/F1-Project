"""Phase 8 — background jobs: lifecycle (success, retry, permanent failure, exhausted retries),
both job stores, the 202 ingest API, and a real RQ round trip on Redis."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from app.api import deps
from app.ingestion.errors import SessionNotFoundError
from app.ingestion.fastf1_client import FastF1Client
from app.jobs.queues import InlineQueue
from app.jobs.runner import JobSystem, PermanentJobError
from app.jobs.store import MemoryJobStore, PostgresJobStore, new_job
from app.jobs.system import make_ingest_handler
from app.main import app
from app.storage.repository import ParquetRepository
from tests import ingestion_synth as synth
from tests import repository_backends as backends

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def system(handlers, defer=False) -> JobSystem:
    queue = InlineQueue(defer_retries=defer)
    js = JobSystem(store=MemoryJobStore(), queue=queue, handlers=handlers, sleep=lambda s: None)
    queue.run = js.run_job
    return js


# ---------------------------------------------------------------- lifecycle
def test_success_records_progress_and_result():
    seen = []

    def handler(job, progress):
        for f in (0.2, 0.1, 0.6):                   # 0.1 must not move the bar backwards
            progress(f, f"step {f}")
            seen.append(js.store.get(job.id).progress)
        return {"ok": True}

    js = system({"ingest": handler})
    job = js.submit("ingest", "2024_05_Q")
    done = js.store.get(job.id)
    assert done.status == "succeeded" and done.progress == 1.0 and done.result == {"ok": True}
    assert done.attempts == 1 and seen == [0.2, 0.2, 0.6]


def test_transient_failure_is_retried_then_succeeds():
    calls = []

    def flaky(job, progress):
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("FastF1 timed out")
        return {"laps": 3}

    js = system({"ingest": flaky})
    done = js.store.get(js.submit("ingest", "2024_05_Q").id)
    assert done.status == "succeeded" and done.attempts == 2 and done.error is None


def test_retries_are_deferred_with_backoff():
    def boom(job, progress):
        raise ConnectionError("down")

    js = system({"ingest": boom}, defer=True)
    job = js.submit("ingest", "2024_05_Q")
    after_first = js.store.get(job.id)
    assert after_first.status == "retrying" and after_first.run_after is not None
    assert after_first.error == "ConnectionError: down"
    assert js.queue.deferred == [(job.id, 5.0)]                 # first backoff
    js.run_job(job.id)
    assert js.queue.deferred[-1] == (job.id, 20.0)              # then longer
    final = js.run_job(job.id)
    assert final.status == "failed" and final.attempts == 3     # max_attempts reached


def test_permanent_errors_fail_at_once():
    def missing(job, progress):
        raise PermanentJobError("session does not exist")

    js = system({"ingest": missing})
    done = js.store.get(js.submit("ingest", "2024_99_Q").id)
    assert done.status == "failed" and done.attempts == 1
    assert done.error == "PermanentJobError: session does not exist"


def test_unknown_kind_and_duplicate_submissions():
    js = system({})
    assert js.store.get(js.submit("train", "x").id).status == "failed"
    store = MemoryJobStore()
    blocked = JobSystem(store=store, queue=InlineQueue(defer_retries=True, run=lambda _id: None), handlers={})
    first = blocked.submit("ingest", "2024_05_Q")
    again = blocked.submit("ingest", "2024_05_Q")             # still queued: the same job comes back
    assert again.id == first.id


def test_a_finished_job_is_not_run_again():
    calls = []
    js = system({"ingest": lambda job, p: calls.append(1) or {}})
    job = js.submit("ingest", "2024_05_Q")
    js.run_job(job.id)
    assert len(calls) == 1


# ---------------------------------------------------------------- stores
@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return MemoryJobStore()
    backends.database_url()
    backends.truncate_all()
    return PostgresJobStore(backends.engine())


@pytest.fixture(scope="module", autouse=True)
def _stop_container():
    yield
    backends.stop()
    from tests import test_cache                      # the RQ test borrows its Redis container
    container = test_cache._REDIS.pop("container", None)
    if container is not None:
        container.stop()
    test_cache._REDIS.clear()


def test_job_store_contract(store):
    job = store.create(new_job("ingest", "2024_05_Q", {"force": True}))
    assert store.get(job.id).params == {"force": True}
    assert store.active_for("ingest", "2024_05_Q").id == job.id
    store.update(job.id, status="running", progress=0.5, message="half")
    got = store.get(job.id)
    assert (got.status, got.progress, got.message) == ("running", 0.5, "half")
    assert got.updated_at >= got.created_at
    store.update(job.id, status="succeeded", result={"laps": 3})
    assert store.active_for("ingest", "2024_05_Q") is None and store.get(job.id).result == {"laps": 3}
    assert store.get("0" * 32) is None


# ---------------------------------------------------------------- the API
@pytest.fixture
def source():
    src = MagicMock(spec=FastF1Client)
    src.fastf1_version = "3.5.3"
    src.load_session.side_effect = lambda y, r, c: synth.raw_session(y, r, c)
    src.lap_telemetry.side_effect = lambda raw, d, n: synth.raw_telemetry_frame()
    return src


@pytest.fixture
async def api(tmp_path, source, as_admin):            # ingest and job status are admin-only (8)
    repo = ParquetRepository(tmp_path)
    js = system({"ingest": make_ingest_handler(lambda: repo, lambda: source)})
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_job_system] = lambda: js
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, js
    app.dependency_overrides.clear()


async def test_ingest_returns_202_with_a_job_and_polling_shows_the_result(api, source):
    client, js = api
    r = await client.post("/api/v1/sessions/2024-05-Q/ingest")
    assert r.status_code == 202
    job = r.json()
    assert r.headers["location"] == f"/api/v1/jobs/{job['id']}" == job["status_url"]
    polled = (await client.get(job["status_url"])).json()
    assert polled["status"] == "succeeded" and polled["progress"] == 1.0
    assert polled["result"]["laps"] == 3 and polled["result"]["skipped"] is False
    assert (await client.get("/api/v1/sessions/2024-05-Q")).json()["status"] == "ready"
    again = (await client.get((await client.post("/api/v1/sessions/2024-05-Q/ingest")).json()["status_url"])).json()
    assert again["result"]["skipped"] is True and source.load_session.call_count == 1
    forced = await client.post("/api/v1/sessions/2024-05-Q/ingest?force=true")
    assert (await client.get(forced.json()["status_url"])).json()["result"]["skipped"] is False


async def test_ingest_of_a_session_that_does_not_exist_fails_once(api, source):
    client, js = api
    source.load_session.side_effect = SessionNotFoundError("FastF1 could not load 2024 round 99 Q")
    job = (await client.post("/api/v1/sessions/2024-99-Q/ingest")).json()
    polled = (await client.get(job["status_url"])).json()
    assert polled["status"] == "failed" and polled["attempts"] == 1
    assert "could not load 2024 round 99 Q" in polled["error"]


async def test_unknown_and_malformed_job_ids(api):
    client, _ = api
    r = await client.get("/api/v1/jobs/" + "0" * 32)
    assert r.status_code == 404 and r.json()["error"]["code"] == "job_not_found"
    assert (await client.get("/api/v1/jobs/nope")).status_code == 422


# ---------------------------------------------------------------- RQ on real Redis
def test_rq_round_trip(tmp_path, monkeypatch):
    from tests.test_cache import redis_url
    url = redis_url()
    import redis
    from rq import SimpleWorker
    from rq.timeouts import TimerDeathPenalty
    from app.jobs import system as system_mod
    from app.jobs.queues import QUEUE_NAME, RQQueue

    store = MemoryJobStore()
    js = JobSystem(store=store, queue=RQQueue(url), handlers={"ingest": lambda job, p: {"via": "rq"}})
    monkeypatch.setattr(system_mod, "get_job_system", lambda: js)
    redis.Redis.from_url(url).flushdb()
    job = js.submit("ingest", "2024_05_Q")
    assert store.get(job.id).status == "queued"

    class Worker(SimpleWorker):
        death_penalty_class = TimerDeathPenalty

    worker = Worker([QUEUE_NAME], connection=redis.Redis.from_url(url))
    if sys.platform == "win32":
        worker.death_penalty_class = TimerDeathPenalty
    worker.work(burst=True)
    done = store.get(job.id)
    assert done.status == "succeeded" and done.result == {"via": "rq"}
