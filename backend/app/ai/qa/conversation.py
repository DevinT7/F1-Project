"""Phase 7E conversation store: the last 6 turns per conversation_id, server-side.

In-memory for now (Redis in Phase 8, same interface). A turn holds the question and the answer
that was RETURNED (validated LLM text, or a template / refusal); failed attempts never enter it.
A conversation is bound to one FactSheet: a different hash starts it afresh.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field

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
