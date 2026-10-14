"""Phase 8 object storage for model artifacts and processed datasets.

  LocalObjectStore  a directory (default; models/ in a checkout)
  S3ObjectStore     any S3-compatible service: MinIO locally (docker-compose), S3 in production

    RACEIQ_OBJECT_STORE = local | s3
    RACEIQ_S3_BUCKET, RACEIQ_S3_ENDPOINT_URL (MinIO), RACEIQ_S3_REGION
    credentials: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from the environment only

Keys are POSIX-style and never escape the store ("..", absolute paths and backslashes are refused).
Every put returns the SHA-256 the registry records; readers verify it.
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,511}$")


class ObjectNotFoundError(LookupError):
    pass


def check_key(key: str) -> str:
    if not _KEY.match(key) or ".." in key.split("/") or "//" in key:
        raise ValueError(f"invalid object key {key!r}")
    return key


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ObjectStore(Protocol):
    name: str

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str = "") -> list[str]: ...


class LocalObjectStore:
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"object key escapes the store: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        from .storage.repository import atomic_write_bytes
        atomic_write_bytes(self._path(key), data)
        return sha256_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> list[str]:
        if not self.root.exists():
            return []
        keys = [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]
        return sorted(k for k in keys if k.startswith(prefix))


class S3ObjectStore:
    name = "s3"

    def __init__(self, bucket: str, *, endpoint_url: str | None = None, region: str | None = None,
                 client: Any | None = None, create_bucket: bool = False) -> None:
        import boto3
        self.bucket = bucket
        self.client = client or boto3.client("s3", endpoint_url=endpoint_url, region_name=region or "us-east-1")
        if create_bucket:
            self.ensure_bucket()

    def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def put_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        digest = sha256_bytes(data)
        self.client.put_object(Bucket=self.bucket, Key=check_key(key), Body=data, ContentType=content_type,
                               Metadata={"sha256": digest})
        return digest

    def get_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=check_key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectNotFoundError(key) from exc
            raise
        data: bytes = obj["Body"].read()
        return data

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=check_key(key))
            return True
        except ClientError:
            return False

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return sorted(keys)


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    from .config import settings
    if settings.object_store == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("RACEIQ_OBJECT_STORE=s3 needs RACEIQ_S3_BUCKET")
        return S3ObjectStore(settings.s3_bucket, endpoint_url=settings.s3_endpoint_url or os.environ.get("AWS_ENDPOINT_URL"),
                             region=settings.s3_region, create_bucket=True)
    return LocalObjectStore(Path(settings.tire_registry_path).parent)
