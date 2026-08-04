"""
Phase 6 (Person 3): the cost guarantees, asserted instead of assumed.

Two promises the plan makes and nothing else checks:

* the model artifact is loaded **once**, not per request (`_artifact` is `lru_cache`d —
  a test so that decorator can't be dropped by accident), and
* one `/api/tires/degradation` call makes a **bounded** number of OpenF1 calls. The
  provider's free tier is 3 req/s; the client rate-limits and caches (see
  `test_phase5_hardening.py`), but a service that fans out per horizon step would still
  turn one dashboard render into a stall.

Offline on the Phase 0 fixtures.

    cd backend && pytest -q tests/test_phase6_cost.py
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from app.services import tires as tires_svc
from tests.fixtures.fake_openf1 import FakeOpenF1Client
# reuse Phase 4's artifact builder rather than growing a second copy of it
from tests.test_phase4_model_serving import _use, _write_artifact

SESSION_KEY, DRIVER = 9159, 16
# OpenF1's free tier. One request must fit inside a single second's budget without
# the limiter having to sleep.
RATE_LIMIT_PER_SECOND = 3

pytest.importorskip("sklearn", reason="the model path needs sklearn to unpickle")
joblib = pytest.importorskip("joblib")


@pytest.fixture(autouse=True)
def _no_cached_artifact():
    tires_svc._artifact.cache_clear()
    yield
    tires_svc._artifact.cache_clear()


class CountingClient(FakeOpenF1Client):
    """Same fixtures, but tallies which endpoints a request touches."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()

    def _tally(self, name: str) -> None:
        self.calls[name] += 1

    async def laps(self, *a, **kw):
        self._tally("laps")
        return await super().laps(*a, **kw)

    async def stints(self, *a, **kw):
        self._tally("stints")
        return await super().stints(*a, **kw)

    async def pit(self, *a, **kw):
        self._tally("pit")
        return await super().pit(*a, **kw)

    async def weather(self, *a, **kw):
        self._tally("weather")
        return await super().weather(*a, **kw)

    async def sessions(self, *a, **kw):
        self._tally("sessions")
        return await super().sessions(*a, **kw)

    async def drivers(self, *a, **kw):
        self._tally("drivers")
        return await super().drivers(*a, **kw)


def _run(monkeypatch, client, **kw):
    monkeypatch.setattr(tires_svc, "get_client", lambda: client)
    return asyncio.run(tires_svc.analyse_current_stint(SESSION_KEY, DRIVER, **kw))


# ------------------------------------------------------------- artifact load count

def test_artifact_is_loaded_once_across_requests(monkeypatch, tmp_path):
    """Unpickling a sklearn Pipeline per request would put disk I/O on every
    dashboard refresh."""
    path = _write_artifact(tmp_path, beats_baseline=True)
    _use(monkeypatch, path)

    loads = Counter()
    real_load = joblib.load

    def counting_load(p, *a, **kw):
        loads["load"] += 1
        return real_load(p, *a, **kw)

    monkeypatch.setattr(joblib, "load", counting_load)

    for _ in range(3):
        assert _run(monkeypatch, CountingClient()).model_name == "tiny-rf"
    assert loads["load"] == 1


def test_a_missing_artifact_is_not_retried_every_request(monkeypatch, tmp_path):
    """The no-model case is the *normal* state of a fresh clone, so the miss has to be
    cached too — otherwise every request pays a failed open()."""
    _use(monkeypatch, tmp_path / "does-not-exist.pkl")

    loads = Counter()
    real_load = joblib.load

    def counting_load(p, *a, **kw):
        loads["load"] += 1
        return real_load(p, *a, **kw)

    monkeypatch.setattr(joblib, "load", counting_load)

    for _ in range(3):
        assert _run(monkeypatch, CountingClient()).model_name == "baseline-linear"
    assert loads["load"] == 1


# --------------------------------------------------------------- openf1 fan-out

def test_baseline_request_fits_in_one_second_of_rate_limit(monkeypatch, tmp_path):
    """laps + stints + pit, and nothing else, when no model is served."""
    _use(monkeypatch, tmp_path / "does-not-exist.pkl")
    client = CountingClient()
    _run(monkeypatch, client)

    assert sum(client.calls.values()) <= RATE_LIMIT_PER_SECOND
    assert set(client.calls) == {"laps", "stints", "pit"}


def test_model_request_fetches_each_endpoint_at_most_once(monkeypatch, tmp_path):
    """The model path adds sessions/drivers/weather for the feature row. That's over
    the per-second budget, so the client's cache is what keeps a warm session cheap —
    but the service must at least not re-fetch anything, and must not scale with the
    horizon."""
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=True))
    client = CountingClient()
    _run(monkeypatch, client, horizon=30)

    assert client.calls, "the model path fetched nothing at all"
    assert max(client.calls.values()) == 1, f"duplicate fetches: {client.calls}"


def test_openf1_cost_does_not_grow_with_the_horizon(monkeypatch, tmp_path):
    """One prediction call covers the whole horizon; a per-lap loop would multiply
    every upstream fetch by `horizon`."""
    _use(monkeypatch, _write_artifact(tmp_path, beats_baseline=True))

    short, long = CountingClient(), CountingClient()
    _run(monkeypatch, short, horizon=5)
    _run(monkeypatch, long, horizon=50)
    assert short.calls == long.calls
