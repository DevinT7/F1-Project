"""Tyre model registry (Phase 6E; Phase 8 adds the database-backed registry).

  ModelRegistry     models/registry.json, artifacts as files next to it
  DbModelRegistry   the model_registry table, artifacts in object storage (MinIO / S3) by key


Each entry: name, version, kind, artifact path + SHA-256, calibration residual file + SHA-256,
dataset hash, git SHA, trained-through date, metrics summary, status (production | candidate).
Paths are relative to the registry file. Loading verifies both checksums (mismatch →
ChecksumMismatchError); a missing registry, entry or file → ModelNotAvailableError (HTTP 503).
Models load lazily on first use and stay cached in memory, keyed by version and checksum.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .serving import FORMAT, ServedModel, point_model_from_json, residuals_from_npz


class ModelNotAvailableError(Exception):
    """No usable tyre model (registry, entry or artifact missing). HTTP 503."""


class ChecksumMismatchError(ModelNotAvailableError):
    """An artifact's SHA-256 does not match the registry: refuse to serve it."""


class RegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    kind: str
    status: Literal["production", "candidate"]
    artifact: str
    sha256: str
    calibration_residuals: str
    calibration_sha256: str
    dataset_hash: str
    git_sha: str
    trained_through: str = Field(description="date of the last race in the training data (YYYY-MM-DD)")
    trained_through_session: str
    alpha: float
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: str = "raceiq-tire-registry/1"
    models: list[RegistryEntry]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Registry:
    """Shared by both registries: production selection, checksum-verified lazy loading, cache."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], ServedModel] = {}
        self._lock = threading.Lock()

    def entries(self) -> list[RegistryEntry]:
        raise NotImplementedError

    def _read(self, ref: str) -> bytes:
        raise NotImplementedError

    def production(self) -> RegistryEntry:
        prod = [e for e in self.entries() if e.status == "production"]
        if not prod:
            raise ModelNotAvailableError("no tyre model is marked production in the registry")
        if len(prod) > 1:
            raise ModelNotAvailableError(f"{len(prod)} tyre models are marked production; exactly one is allowed")
        return prod[0]

    def _verified_bytes(self, ref: str, expected: str) -> bytes:
        data = self._read(ref)
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ChecksumMismatchError(f"checksum mismatch for {ref}: registry {expected[:12]}…, file {actual[:12]}…")
        return data

    def load(self, entry: RegistryEntry) -> ServedModel:
        key = (entry.version, entry.sha256, entry.calibration_sha256)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            spec = json.loads(self._verified_bytes(entry.artifact, entry.sha256))
            if spec.get("format") != FORMAT:
                raise ModelNotAvailableError(f"unsupported artifact format {spec.get('format')!r}")
            residuals = residuals_from_npz(self._verified_bytes(entry.calibration_residuals, entry.calibration_sha256))
            if not residuals:
                raise ModelNotAvailableError("calibration residual file is empty")
            model = ServedModel.build(point_model_from_json(spec["point_model"]), entry.alpha, residuals)
            self._cache[key] = model
            return model


class ModelRegistry(Registry):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)

    def entries(self) -> list[RegistryEntry]:
        if not self.path.is_file():
            raise ModelNotAvailableError(f"tyre model registry {self.path.name} not found on the server")
        try:
            return RegistryFile.model_validate_json(self.path.read_text(encoding="utf-8")).models
        except ValueError as exc:
            raise ModelNotAvailableError(f"tyre model registry {self.path.name} is invalid: {exc}") from exc

    def _read(self, relative: str) -> bytes:
        path = (self.path.parent / relative).resolve()
        if not path.is_relative_to(self.path.parent.resolve()):
            raise ModelNotAvailableError(f"artifact path escapes the registry directory: {relative}")
        if not path.is_file():
            raise ModelNotAvailableError(f"tyre model artifact missing: {relative}")
        return path.read_bytes()


# ---------------------------------------------------------------- Phase 8: database + object storage
_META_FIELDS = ("calibration_residuals", "calibration_sha256", "dataset_hash", "git_sha", "trained_through",
                "trained_through_session", "alpha", "metrics", "notes")


class DbModelRegistry(Registry):
    """model_registry rows (name, version, kind, status, object_key, sha256, metadata) whose
    artifacts live in object storage. Retired versions are never served or listed."""

    def __init__(self, engine: Any, objects: Any) -> None:
        super().__init__()
        self.engine = engine
        self.objects = objects

    def entries(self) -> list[RegistryEntry]:
        from sqlalchemy import select
        from ..db.schema import model_registry as t
        with self.engine.connect() as conn:
            rows = conn.execute(select(t).where(t.c.status.in_(("production", "candidate")))
                                .order_by(t.c.name, t.c.version)).mappings().all()
        if not rows:
            raise ModelNotAvailableError("the model registry has no servable tyre models")
        return [RegistryEntry(name=r["name"], version=r["version"], kind=r["kind"], status=r["status"],
                              artifact=r["object_key"], sha256=r["sha256"],
                              **{k: r["metadata"][k] for k in _META_FIELDS if k in r["metadata"]})
                for r in rows]

    def _read(self, key: str) -> bytes:
        from ..objectstore import ObjectNotFoundError
        try:
            data: bytes = self.objects.get_bytes(key)
        except ObjectNotFoundError as exc:
            raise ModelNotAvailableError(f"tyre model artifact missing in object storage: {key}") from exc
        return data


def publish_entry(engine: Any, objects: Any, entry: RegistryEntry, artifact: bytes, residuals: bytes,
                  prefix: str = "models/tires") -> RegistryEntry:
    """Upload an entry's artifacts (keyed by name/version) and upsert its registry row. Promoting
    to production demotes the previous production version to candidate in the same transaction."""
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert
    from ..db.schema import model_registry as t
    base = f"{prefix}/{entry.name}/{entry.version}"
    art_key, res_key = f"{base}/model.json", f"{base}/residuals.npz"
    art_sha = objects.put_bytes(art_key, artifact, "application/json")
    res_sha = objects.put_bytes(res_key, residuals)
    if art_sha != entry.sha256 or res_sha != entry.calibration_sha256:
        raise ChecksumMismatchError(f"uploaded artifacts for {entry.name} {entry.version} do not match the registry")
    meta = {k: getattr(entry, k) for k in _META_FIELDS} | {"calibration_residuals": res_key}
    row = {"name": entry.name, "version": entry.version, "kind": entry.kind, "status": entry.status,
           "object_key": art_key, "sha256": art_sha, "metadata": meta}
    stmt = insert(t).values(**row)
    stmt = stmt.on_conflict_do_update(index_elements=[t.c.name, t.c.version],
                                      set_={k: stmt.excluded[k] for k in ("kind", "status", "object_key", "sha256", "metadata")})
    with engine.begin() as conn:
        if entry.status == "production":
            conn.execute(update(t).where(t.c.status == "production").values(status="candidate"))
        conn.execute(stmt)
    return entry.model_copy(update={"artifact": art_key, "calibration_residuals": res_key})
