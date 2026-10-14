"""Phase 8: publish the file registry (models/registry.json + artifacts) to object storage and
the model_registry table, so RACEIQ_MODEL_REGISTRY=db serves the same models.

    cd backend && python -m scripts.publish_models [--registry ../models/registry.json]

Idempotent (rows are upserted by name/version; uploads are content-addressed by version).
Checksums are verified on upload. Uses RACEIQ_DATABASE_URL and the configured object store.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.db.engine import make_engine, upgrade
from app.objectstore import ObjectStore, get_object_store
from app.tires.registry import ModelRegistry, RegistryEntry, publish_entry


def publish(registry_path: Path, engine: object, objects: ObjectStore) -> list[RegistryEntry]:
    source = ModelRegistry(registry_path)
    out = []
    for entry in source.entries():
        artifact = source._verified_bytes(entry.artifact, entry.sha256)
        residuals = source._verified_bytes(entry.calibration_residuals, entry.calibration_sha256)
        out.append(publish_entry(engine, objects, entry, artifact, residuals))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.publish_models")
    parser.add_argument("--registry", default=settings.tire_registry_path)
    args = parser.parse_args(argv)
    if not settings.database_url:
        print("set RACEIQ_DATABASE_URL", file=sys.stderr)
        return 2
    upgrade(settings.database_url)
    for e in publish(Path(args.registry), make_engine(settings.database_url), get_object_store()):
        print(f"{e.status:10} {e.name} {e.version} -> {e.artifact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
