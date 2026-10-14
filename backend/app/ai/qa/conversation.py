"""Phase 7E conversation store: the last 6 turns per conversation_id, server-side.

ConversationStore is in memory (one process); RedisConversationStore (Phase 8) is the same interface
on Redis, shared by every API worker. A turn holds the question and the answer
that was RETURNED (validated LLM text, or a template / refusal); failed attempts never enter it.
A conversation is bound to one FactSheet: a different hash starts it afresh.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

from ..llm_client import Message
from .intents import Entities, Intent, PreviousTurn

MAX_TURNS = 6
MAX_CONVERSATIONS = 1000
TTL_S = 6 * 3600


@dataclass(frozen=True)
class Turn:
    question: str
    intent: Intent
    entities: Entities
    answer_text: str                 # validated answer text only
    fact_ids: tuple[str, ...] = ()


@dataclass
class Conversation:
    factsheet_hash: str
    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    touched: float = field(default_factory=time.monotonic)

    def previous(self) -> PreviousTurn | None:
        if not self.turns:
            return None
        last = self.turns[-1]
        return PreviousTurn(last.intent, last.entities)

    def history(self) -> list[Message]:
        out: list[Message] = []
        for t in self.turns:
            out += [Message("user", t.question), Message("assistant", t.answer_text)]
        return out


class ConversationStore:
    def __init__(self, max_turns: int = MAX_TURNS, max_conversations: int = MAX_CONVERSATIONS,
                 ttl_s: float = TTL_S) -> None:
        self._max_turns = max_turns
        self._max = max_conversations
        self._ttl = ttl_s
        self._data: OrderedDict[str, Conversation] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def get(self, conversation_id: str, factsheet_hash: str) -> Conversation:
        with self._lock:
            now = time.monotonic()
            conv = self._data.get(conversation_id)
            if conv is None or conv.factsheet_hash != factsheet_hash or now - conv.touched > self._ttl:
                conv = Conversation(factsheet_hash, deque(maxlen=self._max_turns))
                self._data[conversation_id] = conv
            conv.touched = now
            self._data.move_to_end(conversation_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
            return conv

    def append(self, conversation_id: str, factsheet_hash: str, turn: Turn) -> None:
        conv = self.get(conversation_id, factsheet_hash)
        with self._lock:
            conv.turns.append(turn)

    def turns(self, conversation_id: str) -> list[Turn]:
        with self._lock:
            conv = self._data.get(conversation_id)
            return list(conv.turns) if conv else []

    def __len__(self) -> int:
        return len(self._data)


DEFAULT_STORE = ConversationStore()


# ---------------------------------------------------------------- Phase 8: shared by every worker
class RedisConversationStore:
    """The same interface on Redis, so a conversation works on any API worker:
    raceiq:v1:conv:{id} holds the FactSheet hash, raceiq:v1:conv:{id}:turns the last 6 turns (JSON),
    both with a 6 h sliding TTL. Only validated answers are ever appended (as in memory)."""

    PREFIX = "raceiq:v1:conv"

    def __init__(self, client: Any, max_turns: int = MAX_TURNS, ttl_s: int = TTL_S) -> None:
        self.client = client
        self._max_turns = max_turns
        self._ttl = int(ttl_s)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def _keys(self, conversation_id: str) -> tuple[str, str]:
        return f"{self.PREFIX}:{conversation_id}", f"{self.PREFIX}:{conversation_id}:turns"

    def get(self, conversation_id: str, factsheet_hash: str) -> Conversation:
        meta, turns_key = self._keys(conversation_id)
        stored = self.client.get(meta)
        if stored is None or stored.decode() != factsheet_hash:      # new, expired or another comparison
            pipe = self.client.pipeline()
            pipe.delete(turns_key)
            pipe.set(meta, factsheet_hash, ex=self._ttl)
            pipe.execute()
            return Conversation(factsheet_hash, deque(maxlen=self._max_turns))
        raw = self.client.lrange(turns_key, -self._max_turns, -1)
        self.client.expire(meta, self._ttl)
        self.client.expire(turns_key, self._ttl)
        return Conversation(factsheet_hash, deque((_turn_from_json(r) for r in raw), maxlen=self._max_turns))

    def append(self, conversation_id: str, factsheet_hash: str, turn: Turn) -> None:
        meta, turns_key = self._keys(conversation_id)
        pipe = self.client.pipeline()
        pipe.set(meta, factsheet_hash, ex=self._ttl)
        pipe.rpush(turns_key, _turn_to_json(turn))
        pipe.ltrim(turns_key, -self._max_turns, -1)
        pipe.expire(turns_key, self._ttl)
        pipe.execute()

    def turns(self, conversation_id: str) -> list[Turn]:
        return [_turn_from_json(r) for r in self.client.lrange(self._keys(conversation_id)[1], 0, -1)]


def _turn_to_json(turn: Turn) -> str:
    import json
    return json.dumps({"q": turn.question, "intent": turn.intent.value, "entities": turn.entities.as_dict(),
                       "a": turn.answer_text, "ids": list(turn.fact_ids)})


def _turn_from_json(raw: bytes | str) -> Turn:
    import json
    d = json.loads(raw)
    return Turn(d["q"], Intent(d["intent"]), Entities(**d["entities"]), d["a"], tuple(d["ids"]))


def get_conversation_store() -> ConversationStore | RedisConversationStore:
    """Redis when RACEIQ_REDIS_URL is set (shared by every worker), else this process's memory."""
    from ...config import settings
    if settings.redis_url:
        import redis
        return RedisConversationStore(redis.Redis.from_url(settings.redis_url))
    return DEFAULT_STORE
