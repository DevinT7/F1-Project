"""Phase 8 training job (kind "train", admin only): rebuild the servable tyre models, then publish
them to object storage + the model_registry table.

    params: {"production": "<kind>" (optional), "data_dir": "<path>" (optional)}

The build is scripts/build_tire_registry (6E: split-conformal intervals, checksums, provenance);
publishing verifies every uploaded artifact against its recorded SHA-256. With the file registry
(RACEIQ_MODEL_REGISTRY=file) the rebuilt models/registry.json is what the API serves.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import settings

Progress = Callable[[float, str], None]


def build_models(params: dict[str, Any]) -> None:
    import importlib
    main = importlib.import_module("scripts.build_tire_registry").main
    argv = ["--allow-dirty"]
    if params.get("production"):
        argv += ["--production", str(params["production"])]
    if params.get("data_dir"):
        argv += ["--data-dir", str(params["data_dir"])]
    if main(argv) != 0:
        raise RuntimeError("build_tire_registry failed")


def train_and_register(params: dict[str, Any], progress: Progress) -> dict[str, Any]:
    from pathlib import Path
    progress(0.05, "building the tyre models")
    build_models(params)
    progress(0.80, "publishing to the model registry")
    published: list[dict[str, Any]] = []
    if settings.model_registry == "db" and settings.database_url:
        import importlib
        publish = importlib.import_module("scripts.publish_models").publish
        from ..db.engine import make_engine
        from ..objectstore import get_object_store
        for e in publish(Path(settings.tire_registry_path), make_engine(settings.database_url), get_object_store()):
            published.append({"name": e.name, "version": e.version, "status": e.status, "object_key": e.artifact})
    else:
        from .registry import ModelRegistry
        published = [{"name": e.name, "version": e.version, "status": e.status}
                     for e in ModelRegistry(settings.tire_registry_path).entries()]
    progress(1.0, "registered")
    return {"models": published, "registry": settings.model_registry}
