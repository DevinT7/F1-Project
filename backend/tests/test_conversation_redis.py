"""Phase 8 — the Redis conversation store behaves like the in-memory one, across workers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai.qa.conversation import MAX_TURNS, RedisConversationStore, Turn
from app.ai.qa.engine import answer_question
from app.ai.qa.intents import Entities, Intent
from app.ai.schemas import FactSheet

FIXTURES = Path(__file__).parent / "fixtures" / "ai"


@pytest.fixture
def client():
    from tests.test_cache import redis_url
    import redis
    c = redis.Redis.from_url(redis_url())
    c.flushdb()
    return c


@pytest.fixture(scope="module", autouse=True)
def _stop_redis():
    yield
    from tests import test_cache
    container = test_cache._REDIS.pop("container", None)
    if container is not None:
        container.stop()
    test_cache._REDIS.clear()


def turn(i: int) -> Turn:
    return Turn(f"q{i}", Intent.CORNER_DETAIL, Entities(corners=[f"T{i}"]), f"a{i}", (f"T{i}.time_loss_s",))


def test_round_trip_truncation_and_ttl(client):
    store = RedisConversationStore(client)
    store.get("c1", "hash-a")
    for i in range(MAX_TURNS + 2):
        store.append("c1", "hash-a", turn(i))
    turns = store.turns("c1")
    assert [t.question for t in turns] == [f"q{i}" for i in range(2, MAX_TURNS + 2)]
    assert turns[0].entities.corners == ["T2"] and turns[0].intent is Intent.CORNER_DETAIL
    conv = store.get("c1", "hash-a")
    assert conv.previous().entities.corners == [f"T{MAX_TURNS + 1}"]
    assert [m.role for m in conv.history()][:2] == ["user", "assistant"]
    assert 0 < client.ttl("raceiq:v1:conv:c1") <= 6 * 3600


def test_shared_by_two_workers_and_reset_by_another_comparison(client):
    worker_a, worker_b = RedisConversationStore(client), RedisConversationStore(client)
    worker_a.get("c2", "hash-a")
    worker_a.append("c2", "hash-a", turn(1))
    assert [t.question for t in worker_b.get("c2", "hash-a").turns] == ["q1"]      # another worker sees it
    assert list(worker_b.get("c2", "hash-b").turns) == []                          # a different comparison
    assert worker_a.turns("c2") == []


def test_only_validated_answers_are_stored(client):
    store = RedisConversationStore(client)
    fs = FactSheet.model_validate(json.loads((FIXTURES / "race_lec_pit_window.factsheet.json").read_text(encoding="utf-8")))
    answer_question(fs, "What happened in Turn 7?", store=store, conversation_id="c3")
    res = answer_question(fs, "and in Turn 8?", store=store, conversation_id="c3")
    assert res.route.method == "follow_up" and res.route.entities.corners == ["T8"]
    stored = [json.loads(r) for r in client.lrange("raceiq:v1:conv:c3:turns", 0, -1)]
    assert [s["q"] for s in stored] == ["What happened in Turn 7?", "and in Turn 8?"]
    assert all(s["a"] for s in stored)
