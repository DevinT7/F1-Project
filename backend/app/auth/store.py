"""Accounts and saved analyses. Postgres (users, saved_analyses) when a database is configured,
else in memory (local dev; accounts vanish on restart). Emails are stored lower-cased."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Engine, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from ..db.schema import saved_analyses, users


class EmailTakenError(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: uuid.UUID
    email: str
    password_hash: str
    role: str = "user"
    token_version: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class SavedAnalysis:
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    params: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AccountStore(Protocol):
    def create_user(self, email: str, password_hash: str, role: str) -> User: ...
    def get_user(self, user_id: uuid.UUID) -> User | None: ...
    def get_user_by_email(self, email: str) -> User | None: ...
    def update_user(self, user_id: uuid.UUID, **changes: Any) -> User: ...
    def bump_token_version(self, user_id: uuid.UUID) -> User: ...
    def add_analysis(self, user_id: uuid.UUID, title: str, params: dict[str, Any]) -> SavedAnalysis: ...
    def list_analyses(self, user_id: uuid.UUID) -> list[SavedAnalysis]: ...
    def get_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> SavedAnalysis | None: ...
    def delete_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> bool: ...


class MemoryAccountStore:
    def __init__(self) -> None:
        self._users: dict[uuid.UUID, User] = {}
        self._analyses: dict[uuid.UUID, SavedAnalysis] = {}
        self._lock = threading.Lock()

    def create_user(self, email: str, password_hash: str, role: str) -> User:
        email = email.lower()
        with self._lock:
            if any(u.email == email for u in self._users.values()):
                raise EmailTakenError(email)
            user = User(uuid.uuid4(), email, password_hash, role)
            self._users[user.id] = user
            return user

    def get_user(self, user_id: uuid.UUID) -> User | None:
        return self._users.get(user_id)

    def get_user_by_email(self, email: str) -> User | None:
        email = email.lower()
        return next((u for u in self._users.values() if u.email == email), None)

    def update_user(self, user_id: uuid.UUID, **changes: Any) -> User:
        with self._lock:
            user = replace(self._users[user_id], **changes)
            self._users[user_id] = user
            return user

    def bump_token_version(self, user_id: uuid.UUID) -> User:
        with self._lock:
            user = self._users[user_id]
            user = replace(user, token_version=user.token_version + 1)
            self._users[user_id] = user
            return user

    def add_analysis(self, user_id: uuid.UUID, title: str, params: dict[str, Any]) -> SavedAnalysis:
        item = SavedAnalysis(uuid.uuid4(), user_id, title, params)
        with self._lock:
            self._analyses[item.id] = item
        return item

    def list_analyses(self, user_id: uuid.UUID) -> list[SavedAnalysis]:
        return sorted((a for a in self._analyses.values() if a.user_id == user_id), key=lambda a: a.created_at,
                      reverse=True)

    def get_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> SavedAnalysis | None:
        a = self._analyses.get(analysis_id)
        return a if a is not None and a.user_id == user_id else None

    def delete_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> bool:
        with self._lock:
            a = self._analyses.get(analysis_id)
            if a is None or a.user_id != user_id:
                return False
            del self._analyses[analysis_id]
            return True


_USER_COLS = ("id", "email", "password_hash", "role", "token_version", "created_at")
_ANALYSIS_COLS = ("id", "user_id", "title", "params", "created_at")


class PostgresAccountStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _user(row: Any) -> User:
        return User(**{c: row[c] for c in _USER_COLS})

    @staticmethod
    def _analysis(row: Any) -> SavedAnalysis:
        return SavedAnalysis(**{c: row[c] for c in _ANALYSIS_COLS})

    def create_user(self, email: str, password_hash: str, role: str) -> User:
        try:
            with self.engine.begin() as conn:
                row = conn.execute(insert(users).values(id=uuid.uuid4(), email=email.lower(), password_hash=password_hash,
                                                        role=role).returning(*users.c)).mappings().one()
        except IntegrityError as exc:
            raise EmailTakenError(email) from exc
        return self._user(row)

    def get_user(self, user_id: uuid.UUID) -> User | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.id == user_id)).mappings().first()
        return None if row is None else self._user(row)

    def get_user_by_email(self, email: str) -> User | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.email == email.lower())).mappings().first()
        return None if row is None else self._user(row)

    def update_user(self, user_id: uuid.UUID, **changes: Any) -> User:
        with self.engine.begin() as conn:
            row = conn.execute(update(users).where(users.c.id == user_id).values(**changes)
                               .returning(*users.c)).mappings().one()
        return self._user(row)

    def bump_token_version(self, user_id: uuid.UUID) -> User:
        with self.engine.begin() as conn:
            row = conn.execute(update(users).where(users.c.id == user_id)
                               .values(token_version=users.c.token_version + 1).returning(*users.c)).mappings().one()
        return self._user(row)

    def add_analysis(self, user_id: uuid.UUID, title: str, params: dict[str, Any]) -> SavedAnalysis:
        with self.engine.begin() as conn:
            row = conn.execute(insert(saved_analyses).values(id=uuid.uuid4(), user_id=user_id, title=title, params=params)
                               .returning(*saved_analyses.c)).mappings().one()
        return self._analysis(row)

    def list_analyses(self, user_id: uuid.UUID) -> list[SavedAnalysis]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(saved_analyses).where(saved_analyses.c.user_id == user_id)
                                .order_by(saved_analyses.c.created_at.desc())).mappings().all()
        return [self._analysis(r) for r in rows]

    def get_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> SavedAnalysis | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(saved_analyses).where(saved_analyses.c.id == analysis_id,
                                                            saved_analyses.c.user_id == user_id)).mappings().first()
        return None if row is None else self._analysis(row)

    def delete_analysis(self, user_id: uuid.UUID, analysis_id: uuid.UUID) -> bool:
        with self.engine.begin() as conn:
            n = conn.execute(delete(saved_analyses).where(saved_analyses.c.id == analysis_id,
                                                          saved_analyses.c.user_id == user_id)).rowcount
        return bool(n)
