"""Tyre model registry (Phase 6E): models/registry.json (moves to a DB / object storage in Phase 8).

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


class ModelRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache: dict[tuple[str, str, str], ServedModel] = {}
        self._lock = threading.Lock()

    def entries(self) -> list[RegistryEntry]:
        if not self.path.is_file():
            raise ModelNotAvailableError(f"tyre model registry {self.path.name} not found on the server")
        try:
            return RegistryFile.model_validate_json(self.path.read_text(encoding="utf-8")).models
        except ValueError as exc:
            raise ModelNotAvailableError(f"tyre model registry {self.path.name} is invalid: {exc}") from exc

    def production(self) -> RegistryEntry:
        prod = [e for e in self.entries() if e.status == "production"]
        if not prod:
            raise ModelNotAvailableError("no tyre model is marked production in the registry")
        if len(prod) > 1:
            raise ModelNotAvailableError(f"{len(prod)} tyre models are marked production; exactly one is allowed")
        return prod[0]

    def _verified_bytes(self, relative: str, expected: str) -> bytes:
        path = (self.path.parent / relative).resolve()
        if not path.is_relative_to(self.path.parent.resolve()):
            raise ModelNotAvailableError(f"artifact path escapes the registry directory: {relative}")
        if not path.is_file():
            raise ModelNotAvailableError(f"tyre model artifact missing: {relative}")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise ChecksumMismatchError(f"checksum mismatch for {relative}: registry {expected[:12]}…, file {actual[:12]}…")
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
