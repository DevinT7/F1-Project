"""Phase 8 — object storage (local + S3/MinIO) and the database-backed model registry.

S3 comes from RACEIQ_TEST_S3_ENDPOINT (+ AWS_* credentials), else a MinIO container via
testcontainers when Docker runs, else the S3 cases are skipped with the reason. The database
cases use the same provisioning as the repository contract suite.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.db.schema import model_registry
from app.jobs.queues import InlineQueue
from app.jobs.runner import JobSystem
from app.jobs.store import MemoryJobStore
from app.objectstore import LocalObjectStore, ObjectNotFoundError, S3ObjectStore
from app.storage.repository import ParquetRepository
from app.tires.registry import ChecksumMismatchError, DbModelRegistry, ModelNotAvailableError, ModelRegistry
from app.tires.service import TireService
from scripts.publish_models import publish
from tests import repository_backends as backends
from tests.repository_backends import _docker_running

FIXTURE_STORE = Path(__file__).parent / "fixtures" / "ingestion" / "store"
S3_IMAGE = "chrislusf/seaweedfs:4.48"
_S3: dict[str, object] = {}


def s3_store() -> S3ObjectStore:
    endpoint = os.environ.get("RACEIQ_TEST_S3_ENDPOINT")
    if not endpoint and "endpoint" not in _S3:
        if not _docker_running():
            pytest.skip("S3 backend skipped: set RACEIQ_TEST_S3_ENDPOINT or start Docker (MinIO via testcontainers)")
        from testcontainers.core.container import DockerContainer
        # SeaweedFS's S3 gateway (MinIO no longer publishes pullable images; see docs/decisions.md)
        container = (DockerContainer(S3_IMAGE).with_command("server -dir=/data -s3 -s3.port=8333")
                     .with_exposed_ports(8333))
        container.start()
        _S3["container"] = container
        _S3["endpoint"] = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8333)}"
        _S3["creds"] = ("raceiq", "raceiq-secret")
    endpoint = endpoint or str(_S3["endpoint"])
    import boto3
    key, secret = _S3.get("creds") or (os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"])  # type: ignore[misc]
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=key, aws_secret_access_key=secret,
                          region_name="us-east-1")
    deadline = time.monotonic() + 30
    while True:                                          # MinIO takes a moment to accept requests
        try:
            client.list_buckets()
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    store = S3ObjectStore(f"raceiq-test-{int(time.time() * 1000) % 10**9}", client=client, create_bucket=True)
    return store


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    container = _S3.pop("container", None)
    if container is not None:
        container.stop()  # type: ignore[attr-defined]
    _S3.clear()
    backends.stop()


@pytest.fixture(params=["local", "s3"])
def objects(request):
    if request.param == "s3":
        yield s3_store()
        return
    import shutil
    import tempfile
    root = tempfile.mkdtemp(prefix="obj")             # short: deep keys must stay under Windows MAX_PATH
    yield LocalObjectStore(root)
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- object store contract
def test_put_get_exists_list(objects):
    digest = objects.put_bytes("models/tires/a/1/model.json", b'{"x": 1}', "application/json")
    import hashlib
    assert digest == hashlib.sha256(b'{"x": 1}').hexdigest()
    assert objects.get_bytes("models/tires/a/1/model.json") == b'{"x": 1}'
    assert objects.exists("models/tires/a/1/model.json") and not objects.exists("models/tires/a/2/model.json")
    objects.put_bytes("models/tires/b/1/model.json", b"y")
    assert objects.list("models/tires/") == ["models/tires/a/1/model.json", "models/tires/b/1/model.json"]
    with pytest.raises(ObjectNotFoundError):
        objects.get_bytes("models/none.json")


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs", "a/../../b", "a//b", "", "a\\b"])
def test_unsafe_keys_are_refused(objects, key):
    with pytest.raises(ValueError):
        objects.put_bytes(key, b"x")


# ---------------------------------------------------------------- the database registry
@pytest.fixture
def db_registry(objects):
    backends.database_url()
    backends.truncate_all()
    publish(Path(settings.tire_registry_path), backends.engine(), objects)
    return DbModelRegistry(backends.engine(), objects)


def test_published_registry_serves_the_same_models(db_registry):
    file_reg = ModelRegistry(settings.tire_registry_path)
    by_db = {(e.name, e.version): e for e in db_registry.entries()}
    by_file = {(e.name, e.version): e for e in file_reg.entries()}
    assert by_db.keys() == by_file.keys()
    assert db_registry.production().name == file_reg.production().name
    for k, e in by_db.items():
        assert e.sha256 == by_file[k].sha256 and e.metrics == by_file[k].metrics
        assert e.artifact.startswith(f"models/tires/{e.name}/{e.version}/")
    # same predictions from both registries (synthetic race fixture)
    repo = ParquetRepository(FIXTURE_STORE)
    a = TireService(repo, file_reg).predict("2024_99_R", "AAA", 40, 5)
    b = TireService(repo, db_registry).predict("2024_99_R", "AAA", 40, 5)
    from dataclasses import asdict
    assert asdict(a) == asdict(b)


def test_tampered_or_missing_artifacts_are_refused(db_registry, objects):
    entry = db_registry.production()
    objects.put_bytes(entry.artifact, b'{"format": "tampered"}')
    with pytest.raises(ChecksumMismatchError):
        DbModelRegistry(backends.engine(), objects).load(entry)
    with backends.engine().begin() as conn:
        conn.execute(model_registry.update().where(model_registry.c.name == entry.name)
                     .values(object_key="models/tires/gone/model.json"))
    with pytest.raises(ModelNotAvailableError):
        fresh = DbModelRegistry(backends.engine(), objects)
        fresh.load(fresh.production())


def test_promoting_demotes_the_previous_production_and_retired_is_never_served(db_registry, objects):
    from app.tires.registry import publish_entry
    prod = db_registry.production()
    candidate = next(e for e in db_registry.entries() if e.status == "candidate")
    source = ModelRegistry(settings.tire_registry_path)
    src = next(e for e in source.entries() if e.name == candidate.name)
    publish_entry(backends.engine(), objects, src.model_copy(update={"status": "production"}),
                  source._verified_bytes(src.artifact, src.sha256),
                  source._verified_bytes(src.calibration_residuals, src.calibration_sha256))
    assert db_registry.production().name == candidate.name
    with backends.engine().connect() as conn:
        statuses = dict(conn.execute(select(model_registry.c.name, model_registry.c.status)).all())
    assert statuses[prod.name] == "candidate"
    with backends.engine().begin() as conn:
        conn.execute(text("UPDATE model_registry SET status = 'retired' WHERE name = :n"), {"n": prod.name})
    assert prod.name not in {e.name for e in db_registry.entries()}


# ---------------------------------------------------------------- the training job
def test_training_job_builds_publishes_and_records_the_models(monkeypatch, objects):
    backends.database_url()
    backends.truncate_all()
    from app.tires import training_job
    built = []
    monkeypatch.setattr(training_job, "build_models", lambda params: built.append(params))
    monkeypatch.setattr(settings, "model_registry", "db")
    monkeypatch.setattr(settings, "database_url", backends.database_url())
    monkeypatch.setattr("app.objectstore.get_object_store", lambda: objects)
    queue = InlineQueue()
    js = JobSystem(store=MemoryJobStore(), queue=queue,
                   handlers={"train": lambda job, p: training_job.train_and_register(job.params, p)})
    queue.run = js.run_job
    job = js.store.get(js.submit("train", "tire-models", {"production": "pooled_fixed_effects_recent"}).id)
    assert job.status == "succeeded", job.error
    assert built == [{"production": "pooled_fixed_effects_recent"}]
    assert {m["name"] for m in job.result["models"]} == {e.name for e in ModelRegistry(settings.tire_registry_path).entries()}
    assert all(m["object_key"].startswith("models/tires/") for m in job.result["models"])
    assert DbModelRegistry(backends.engine(), objects).production()
